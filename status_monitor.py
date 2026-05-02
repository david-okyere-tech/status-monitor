import requests
import time
from datetime import datetime

def check_status(url, interval=60, max_checks=5):
    """
    Monitor a website's status.
    Checks every X seconds for a set number of times.
    Logs all results to a file.
    """
    print(f"Starting monitor for: {url}")
    print(f"Checking every {interval} seconds, {max_checks} times total.")
    print("Press Ctrl+C to stop early.\n")
    
    log_file = "status_log.txt"
    
    # Write header to log file
    with open(log_file, "w") as f:
        f.write(f"Status Monitor Log - Target: {url}\n")
        f.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("-" * 50 + "\n")
    
    for i in range(1, max_checks + 1):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            response = requests.get(url, timeout=10)
            status_code = response.status_code
            
            if status_code == 200:
                message = f"[{timestamp}] Check {i}: UP - Status {status_code} - OK"
            else:
                message = f"[{timestamp}] Check {i}: WARNING - Status {status_code}"
                
        except requests.exceptions.Timeout:
            message = f"[{timestamp}] Check {i}: DOWN - Request timed out"
        except requests.exceptions.ConnectionError:
            message = f"[{timestamp}] Check {i}: DOWN - Connection failed"
        except requests.exceptions.RequestException as e:
            message = f"[{timestamp}] Check {i}: ERROR - {str(e)}"
        
        # Print to console
        print(message)
        
        # Append to log file
        with open(log_file, "a") as f:
            f.write(message + "\n")
        
        # Wait before next check (unless it's the last one)
        if i < max_checks:
            time.sleep(interval)
    
    print(f"\nMonitoring complete. Log saved to: {log_file}")

if __name__ == "__main__":
    # Get URL from user
    url = input("Enter the URL to monitor (include https://): ").strip()
    
    if not url:
        print("Please provide a valid URL.")
    elif not url.startswith("http"):
        print("URL must start with http:// or https://")
    else:
        check_status(url, interval=5, max_checks=3)