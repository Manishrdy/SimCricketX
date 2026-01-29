"""
Test Script for Stats Enhancement Features
===========================================

This script tests the three new features:
1. Best Bowling Figures Leaderboard
2. Player Comparison Tool
3. Partnership Statistics

Run this after starting the Flask app to verify functionality.
"""

import requests
import json

# Configuration
BASE_URL = "http://127.0.0.1:7860"
# You'll need to set these after logging in
SESSION_COOKIE = None  # Set this manually or automate login

def test_bowling_figures():
    """Test bowling figures API endpoint"""
    print("\n" + "="*60)
    print("Testing: Best Bowling Figures Leaderboard")
    print("="*60)
    
    url = f"{BASE_URL}/api/bowling-figures"
    params = {
        'limit': 10
    }
    
    try:
        response = requests.get(url, params=params)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Success! Found {data.get('count', 0)} bowling figures")
            print("\nTop 5 Bowling Figures:")
            for i, figure in enumerate(data.get('data', [])[:5], 1):
                print(f"  {i}. {figure['player']} ({figure['team']}): {figure['figures']}")
                print(f"     vs {figure['opponent']} | Overs: {figure['overs']} | Econ: {figure['economy']}")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception: {e}")

def test_player_comparison():
    """Test player comparison API endpoint"""
    print("\n" + "="*60)
    print("Testing: Player Comparison Tool")
    print("="*60)
    
    # Note: You'll need to provide actual player IDs from your database
    url = f"{BASE_URL}/api/compare-players"
    params = {
        'player_ids': [1, 2]  # Replace with real player IDs
    }
    
    try:
        response = requests.get(url, params=params)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print("✅ Success! Comparison Data Retrieved")
            
            if 'data' in data and 'players' in data['data']:
                print(f"\nComparing {len(data['data']['players'])} players")
                
                # Batting comparison
                if data['data'].get('batting_comparison'):
                    print("\n📊 Batting Comparison:")
                    for p in data['data']['batting_comparison']:
                        print(f"  {p['player']}: {p['runs']} runs @ {p['average']} avg, SR {p['strike_rate']}")
                
                # Bowling comparison
                if data['data'].get('bowling_comparison'):
                    print("\n🎳 Bowling Comparison:")
                    for p in data['data']['bowling_comparison']:
                        print(f"  {p['player']}: {p['wickets']} wkts @ {p['economy']} econ")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception: {e}")

def test_partnership_stats():
    """Test partnership statistics API endpoint"""
    print("\n" + "="*60)
    print("Testing: Partnership Statistics")
    print("="*60)
    
    # Note: Replace with actual player ID
    player_id = 1
    url = f"{BASE_URL}/api/player/{player_id}/partnerships"
    
    try:
        response = requests.get(url)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print("✅ Success! Partnership Data Retrieved")
            
            if 'data' in data:
                stats = data['data']
                print(f"\nPlayer: {stats.get('player_name')}")
                print(f"Total Partnerships: {stats.get('total_partnerships', 0)}")
                print(f"Average Partnership: {stats.get('average_partnership', 0)} runs")
                
                # Milestones
                if 'milestones' in stats:
                    m = stats['milestones']
                    print(f"\nMilestones:")
                    print(f"  50+ runs: {m.get('50+', 0)}")
                    print(f"  100+ runs: {m.get('100+', 0)}")
                    print(f"  150+ runs: {m.get('150+', 0)}")
                
                # Best partnerships
                if stats.get('best_partnerships'):
                    print(f"\nTop 3 Partnerships:")
                    for i, p in enumerate(stats['best_partnerships'][:3], 1):
                        print(f"  {i}. {p['runs']} runs with {p['partner']}")
                        print(f"     ({p['player_contribution']} + {p['partner_contribution']}) vs {p['opponent']}")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception: {e}")

def test_tournament_partnerships():
    """Test tournament partnership leaderboard"""
    print("\n" + "="*60)
    print("Testing: Tournament Partnership Leaderboard")
    print("="*60)
    
    # Note: Replace with actual tournament ID
    tournament_id = 1
    url = f"{BASE_URL}/api/tournament/{tournament_id}/partnerships"
    params = {'limit': 5}
    
    try:
        response = requests.get(url, params=params)
        print(f"Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Success! Found {data.get('count', 0)} partnerships")
            
            print("\nTop Partnerships in Tournament:")
            for i, p in enumerate(data.get('data', []), 1):
                print(f"  {i}. {p['batsman1']} & {p['batsman2']}: {p['runs']} runs")
                print(f"     ({p['batsman1_contribution']} + {p['batsman2_contribution']}) | {p['wicket']}th wicket")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Exception: {e}")

if __name__ == "__main__":
    print("="*60)
    print("Stats Enhancement Features - API Test Suite")
    print("="*60)
    print("\n⚠️  IMPORTANT: You must be logged in to test these endpoints")
    print("    The tests may return 401/403 if not authenticated\n")
    
    # Run all tests
    test_bowling_figures()
    test_player_comparison()
    test_partnership_stats()
    test_tournament_partnerships()
    
    print("\n" + "="*60)
    print("Test Suite Complete")
    print("="*60)
    print("\nNOTE: Some tests may fail if:")
    print("  - You're not logged in")
    print("  - There's no data in the database")
    print("  - Player/Tournament IDs don't exist")
    print("\nTo fully test, use actual IDs from your database")
