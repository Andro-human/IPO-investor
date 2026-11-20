"""Minimal Upstox API client: TOTP login, IPO endpoints, holdings, market sell."""
import logging
from datetime import datetime, timedelta

import pyotp
import requests

from .state import IST, State, now_ist

log = logging.getLogger(__name__)
BASE = "https://api.upstox.com"
HFT = "https://api-hft.upstox.com"


class UpstoxError(Exception):
    pass


class Upstox:
    def __init__(self, api_key: str, totp_secret: str, pin: str, state: State):
        self.api_key, self.totp_secret, self.pin, self.state = api_key, totp_secret, pin, state
        self._token = None

    # ---------------- auth ----------------
    def token(self) -> str:
        if self._token:
            return self._token
        cached = self.state.read("token.json", {})
        if cached.get("access_token") and cached.get("expires_at"):
            if now_ist() < datetime.fromisoformat(cached["expires_at"]):
                self._token = cached["access_token"]
                return self._token
        return self.login_totp()

    def login_totp(self) -> str:
        """Programmatic login: returns a token valid until 3:30 AM IST next day."""
        if not (self.api_key and self.totp_secret and self.pin):
            raise UpstoxError("UPSTOX_API_KEY / UPSTOX_TOTP_SECRET / UPSTOX_PIN missing")
        totp = pyotp.TOTP(self.totp_secret.replace(" ", "")).now()
        r = requests.post(f"{BASE}/v2/totp-login/authorization/token",
                          headers={"accept": "application/json", "Content-Type": "application/json",
                                   "x-api-key": self.api_key},
                          json={"totp": totp, "pin": str(self.pin)}, timeout=30)
        j = _check_full(r)
        data = j.get("data") if isinstance(j.get("data"), dict) else j  # token responses are top-level
        tok = data.get("access_token")
        if not tok:
            raise UpstoxError(f"no access_token in login response: {_redact(j)}")
        now = now_ist()
        expires = (now + timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0)
        self.state.write("token.json", {"access_token": tok, "obtained_at": now.isoformat(),
                                        "expires_at": expires.isoformat(), "user_id": data.get("user_id")})
        self._token = tok
        log.info("logged in to Upstox as %s", data.get("user_name"))
        return tok

    def _headers(self) -> dict:
        return {"Accept": "application/json", "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token()}"}

    def _request(self, method: str, url: str, **kw) -> dict:
        """Send a request; if the cached token was revoked (another login happened), log in again once."""
        r = requests.request(method, url, headers=self._headers(), timeout=30, **kw)
        if r.status_code == 401:
            log.warning("Upstox token rejected; logging in again")
            self._token = None
            self.login_totp()
            r = requests.request(method, url, headers=self._headers(), timeout=30, **kw)
        return _check_full(r)

    def _get(self, path: str, params=None, base=BASE):
        return self._request("GET", base + path, params=params).get("data")

    def _post(self, path: str, body: dict, base=BASE):
        return self._request("POST", base + path, json=body).get("data")

    def _delete(self, path: str, base=BASE):
        return self._request("DELETE", base + path).get("data")

    # ---------------- account ----------------
    def profile(self) -> dict:
        return self._get("/v2/user/profile")

    def holdings(self) -> list:
        return self._get("/v2/portfolio/long-term-holdings") or []

    # ---------------- IPO ----------------
    def list_ipos(self, status="open", issue_type="regular") -> list:
        out, page = [], 1
        while True:
            j = self._request("GET", f"{BASE}/v2/ipos",
                              params={"status": status, "issue_type": issue_type, "page_number": page, "records": 30})
            out.extend(j.get("data") or [])
            pages = ((j.get("meta_data") or {}).get("page") or {}).get("total_pages", 1)
            if page >= (pages or 1):
                break
            page += 1
        return out

    def ipo_details(self, ipo_id: str) -> dict:
        return self._get(f"/v2/ipos/{ipo_id}")

    def apply_ipo(self, ipo_id: str, upi: str, quantity: int, price: float, category="IND") -> dict:
        body = {"id": ipo_id, "upi": upi, "category": category,
                "bids": [{"quantity": int(quantity), "price": float(price)}]}
        return self._post("/v2/ipos/orders", body)

    def ipo_orders(self) -> list:
        out, page = [], 1
        while True:
            j = self._request("GET", f"{BASE}/v2/ipos/orders", params={"page_number": page, "records": 30})
            out.extend(j.get("data") or [])
            pages = ((j.get("meta_data") or {}).get("page") or {}).get("total_pages", 1)
            if page >= (pages or 1):
                break
            page += 1
        return out

    def ipo_order(self, order_id: str) -> dict:
        return self._get(f"/v2/ipos/orders/{order_id}")

    def cancel_ipo(self, order_id: str) -> dict:
        return self._delete(f"/v2/ipos/orders/{order_id}")

    # ---------------- selling ----------------
    def sell_market(self, instrument_token: str, quantity: int, tag="ipo-bidder") -> dict:
        body = {"quantity": int(quantity), "product": "D", "validity": "DAY", "price": 0, "tag": tag,
                "instrument_token": instrument_token, "order_type": "MARKET", "transaction_type": "SELL",
                "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False, "slice": False,
                "market_protection": -1}
        return self._post("/v3/order/place", body, base=HFT)


def _redact(j: dict) -> dict:
    return {k: ("<redacted>" if "token" in k else v) for k, v in j.items()}


def _check_full(r: requests.Response) -> dict:
    try:
        j = r.json()
    except ValueError:
        raise UpstoxError(f"HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code >= 400 or j.get("status") == "error":
        errs = j.get("errors") or [j]
        msg = "; ".join(f"{e.get('errorCode') or e.get('error_code', '')} {e.get('message', '')}".strip() for e in errs)
        raise UpstoxError(f"HTTP {r.status_code}: {msg or r.text[:300]}")
    return j


def _check(r: requests.Response):
    return _check_full(r).get("data")
