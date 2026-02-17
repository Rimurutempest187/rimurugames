# simple dotenv replacement (no install needed)

import os

def load_dotenv(path=".env"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k,v=line.split("=",1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass
