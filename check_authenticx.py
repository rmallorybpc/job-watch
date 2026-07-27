import requests
r = requests.get("https://boards-api.greenhouse.io/v1/boards/authenticx/jobs")
for j in r.json().get("jobs", []):
    print(j["title"], "|", (j.get("location") or {}).get("name", ""))