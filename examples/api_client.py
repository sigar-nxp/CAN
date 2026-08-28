import requests
import time

# Base URL of the running PS Locks Gateway
BASE_URL = "http://127.0.0.1:8000"
DEVICE_ID = 1

def main():
    """
    Demonstrates how to interact with the gateway's REST API.
    """
    # 1. Check system health
    print("Checking system health...")
    health_resp = requests.get(f"{BASE_URL}/system/health")
    if health_resp.status_code == 200:
        print(f"Gateway is healthy: {health_resp.json()}\n")
    
    # 2. Open a specific lock via HTTP POST
    print(f"Triggering open for Device {DEVICE_ID}...")
    open_resp = requests.post(f"{BASE_URL}/devices/{DEVICE_ID}/open")
    
    if open_resp.status_code == 200:
        print(f"Success! Lock responded: {open_resp.json()}")
    else:
        print(f"Failed to open lock. Status: {open_resp.status_code}")

if __name__ == "__main__":
    main()