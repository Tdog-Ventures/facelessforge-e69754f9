"""App config - Company location Adelaide. Secrets must come from env only."""
import os
COMPANY_LOCATION = os.environ.get("COMPANY_LOCATION", "Adelaide, South Australia")
COMPANY_CITY = "Adelaide"
COMPANY_STATE = "South Australia"
COMPANY_LOCATION_FULL = "Adelaide, South Australia"
ABN = os.environ.get("ABN", "60 578 933 517")
SALES_TAX = os.environ.get("SALES_TAX", "60578933517")
# No default credential: Paddle access requires PADDLE_TOKEN in the environment.
# A hardcoded fallback was removed (production-hardening: secret hygiene).
PADDLE_TOKEN = os.environ.get("PADDLE_TOKEN")
# Location helpers
def get_location():
    return COMPANY_LOCATION
