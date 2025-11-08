#!/usr/bin/env python3
"""
Immich & CloudFlare Monitoring Service
Provides metrics endpoints for monitoring dashboard
"""

import os
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, jsonify
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# Configuration
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'immich')
DB_USER = os.getenv('DB_USER', 'immich')
DB_PASS = os.getenv('DB_PASS', 'immich')

CF_ZONE_ID = os.getenv('CF_ZONE_ID', '38aa1cdf7fdd9ee1a7fc14778549362a')
CF_API_KEY = os.getenv('CF_API_KEY', '9IEtu93TkCiquou0y17uPK4Lc_4XAf8HhNIPJ5Eu')


def get_db_connection():
    """Get PostgreSQL connection"""
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        cursor_factory=RealDictCursor
    )


def get_immich_metrics():
    """Get Immich upload and usage metrics"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Upload frequency metrics
        cur.execute("""
            SELECT
                COUNT(*) as total_assets,
                COUNT(CASE WHEN "createdAt" > NOW() - INTERVAL '1 hour' THEN 1 END) as last_1h,
                COUNT(CASE WHEN "createdAt" > NOW() - INTERVAL '24 hours' THEN 1 END) as last_24h,
                COUNT(CASE WHEN "createdAt" > NOW() - INTERVAL '7 days' THEN 1 END) as last_7d,
                COUNT(CASE WHEN "createdAt" > NOW() - INTERVAL '30 days' THEN 1 END) as last_30d,
                MAX("createdAt") as last_upload
            FROM asset
            WHERE "deletedAt" IS NULL;
        """)
        upload_stats = cur.fetchone()

        # Active users
        cur.execute("""
            SELECT
                COUNT(DISTINCT "ownerId") as active_users_24h
            FROM asset
            WHERE "createdAt" > NOW() - INTERVAL '24 hours'
            AND "deletedAt" IS NULL;
        """)
        user_stats = cur.fetchone()

        # Storage metrics
        cur.execute("""
            SELECT
                COUNT(*) as total_users,
                SUM(CASE WHEN "isAdmin" = true THEN 1 ELSE 0 END) as admin_users
            FROM "user";
        """)
        all_users = cur.fetchone()

        # Calculate upload rate (uploads per hour over last 24h)
        uploads_24h = upload_stats['last_24h'] or 0
        upload_rate_per_hour = round(uploads_24h / 24, 1)

        # Time since last upload
        last_upload = upload_stats['last_upload']
        minutes_since_upload = None
        if last_upload:
            time_diff = datetime.now(last_upload.tzinfo) - last_upload
            minutes_since_upload = int(time_diff.total_seconds() / 60)

        # Determine health
        is_active = minutes_since_upload is not None and minutes_since_upload < 120  # Active if upload in last 2 hours

        cur.close()
        conn.close()

        return {
            'total_assets': upload_stats['total_assets'],
            'uploads': {
                'last_1h': upload_stats['last_1h'],
                'last_24h': upload_stats['last_24h'],
                'last_7d': upload_stats['last_7d'],
                'last_30d': upload_stats['last_30d'],
                'rate_per_hour': upload_rate_per_hour
            },
            'users': {
                'total': all_users['total_users'],
                'admins': all_users['admin_users'],
                'active_24h': user_stats['active_users_24h']
            },
            'last_upload': {
                'timestamp': last_upload.isoformat() if last_upload else None,
                'minutes_ago': minutes_since_upload
            },
            'health': {
                'is_active': is_active,
                'alert': not is_active
            }
        }

    except Exception as e:
        return {'error': str(e)}


def get_cloudflare_metrics():
    """Get CloudFlare analytics and security metrics using GraphQL API"""
    try:
        headers = {
            'Authorization': f'Bearer {CF_API_KEY}',
            'Content-Type': 'application/json'
        }

        # Get analytics for last 24 hours
        now = datetime.utcnow()
        start = (now - timedelta(hours=24)).isoformat() + 'Z'
        end = now.isoformat() + 'Z'

        # GraphQL query for zone analytics (using 1-hour groups for last 24 hours)
        graphql_query = {
            "query": """
query ZoneAnalytics($zoneTag: String!, $start: Time!) {
    viewer {
        zones(filter: { zoneTag: $zoneTag }) {
            httpRequests1hGroups(
                limit: 24
                filter: { datetime_gt: $start }
            ) {
                sum {
                    requests
                    bytes
                    cachedBytes
                    cachedRequests
                    threats
                }
                dimensions {
                    datetime
                }
            }
        }
    }
}
            """,
            "variables": {
                "zoneTag": CF_ZONE_ID,
                "start": start
            }
        }

        # Query GraphQL API
        graphql_url = 'https://api.cloudflare.com/client/v4/graphql'
        response = requests.post(graphql_url, headers=headers, json=graphql_query, timeout=10)

        if response.status_code != 200:
            return {
                'error': f'CloudFlare API returned {response.status_code}',
                'configured': bool(CF_API_KEY and CF_ZONE_ID)
            }

        data = response.json()

        # Check for GraphQL errors
        if 'errors' in data and data['errors']:
            return {'error': 'CloudFlare GraphQL errors', 'details': data.get('errors')}

        # Extract metrics from GraphQL response
        zones = data.get('data', {}).get('viewer', {}).get('zones', [])
        if not zones or not zones[0].get('httpRequests1hGroups'):
            return {'error': 'No analytics data available', 'configured': True}

        # Aggregate hourly data into 24-hour totals
        hourly_groups = zones[0]['httpRequests1hGroups']
        if not hourly_groups:
            # No data available - return zeros but mark as configured
            return {
                'zone': {
                    'name': 'jepson.live',
                    'status': 'unknown',
                    'plan': 'unknown'
                },
                'requests_24h': {
                    'total': 0,
                    'cached': 0,
                    'uncached': 0,
                    'cache_hit_ratio': 0
                },
                'bandwidth_24h': {
                    'total_bytes': 0,
                    'total_gb': 0,
                    'cached_bytes': 0
                },
                'security_24h': {
                    'threats_blocked': 0
                },
                'health': {
                    'zone_active': True,
                    'alert': False
                },
                'configured': True,
                'note': 'No analytics data available for the last 24 hours (free plan delay or low traffic)'
            }

        # Sum all hourly metrics
        metrics = {
            'requests': sum(group['sum']['requests'] for group in hourly_groups),
            'bytes': sum(group['sum']['bytes'] for group in hourly_groups),
            'cachedRequests': sum(group['sum']['cachedRequests'] for group in hourly_groups),
            'cachedBytes': sum(group['sum']['cachedBytes'] for group in hourly_groups),
            'threats': sum(group['sum']['threats'] for group in hourly_groups)
        }

        # Get zone info
        zone_url = f'https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}'
        zone_response = requests.get(zone_url, headers=headers, timeout=10)
        zone_data = zone_response.json() if zone_response.status_code == 200 else {}
        zone_info = zone_data.get('result', {})

        # Calculate derived metrics
        requests_all = metrics.get('requests', 0)
        requests_cached = metrics.get('cachedRequests', 0)
        requests_uncached = requests_all - requests_cached

        cache_hit_ratio = 0
        if requests_all > 0:
            cache_hit_ratio = round((requests_cached / requests_all) * 100, 2)

        bandwidth_all = metrics.get('bytes', 0)
        bandwidth_cached = metrics.get('cachedBytes', 0)
        bandwidth_gb = round(bandwidth_all / (1024**3), 2)

        threats_all = metrics.get('threats', 0)

        return {
            'zone': {
                'name': zone_info.get('name', 'jepson.live'),
                'status': zone_info.get('status', 'unknown'),
                'plan': zone_info.get('plan', {}).get('name', 'unknown')
            },
            'requests_24h': {
                'total': requests_all,
                'cached': requests_cached,
                'uncached': requests_uncached,
                'cache_hit_ratio': cache_hit_ratio
            },
            'bandwidth_24h': {
                'total_bytes': bandwidth_all,
                'total_gb': bandwidth_gb,
                'cached_bytes': bandwidth_cached
            },
            'security_24h': {
                'threats_blocked': threats_all
            },
            'health': {
                'zone_active': zone_info.get('status') == 'active',
                'alert': zone_info.get('status') != 'active'
            },
            'configured': True
        }

    except requests.exceptions.Timeout:
        return {'error': 'CloudFlare API timeout', 'configured': True}
    except Exception as e:
        return {'error': str(e), 'configured': bool(CF_API_KEY and CF_ZONE_ID)}


@app.route('/health', methods=['GET', 'HEAD'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy'})


@app.route('/immich', methods=['GET', 'HEAD'])
def immich_metrics():
    """Immich metrics endpoint"""
    metrics = get_immich_metrics()
    return jsonify(metrics)


@app.route('/cloudflare', methods=['GET', 'HEAD'])
def cloudflare_metrics():
    """CloudFlare metrics endpoint"""
    metrics = get_cloudflare_metrics()
    return jsonify(metrics)


@app.route('/', methods=['GET', 'HEAD'])
@app.route('/all', methods=['GET', 'HEAD'])
def all_metrics():
    """Combined metrics endpoint"""
    return jsonify({
        'timestamp': datetime.utcnow().isoformat(),
        'immich': get_immich_metrics(),
        'cloudflare': get_cloudflare_metrics()
    })


if __name__ == '__main__':
    port = int(os.getenv('PORT', 8082))
    app.run(host='0.0.0.0', port=port, debug=False)
