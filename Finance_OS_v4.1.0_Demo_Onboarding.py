import streamlit as st
from pathlib import Path
from datetime import datetime, date, timedelta
import pandas as pd
import json
import hashlib
import base64
import urllib.request
import urllib.error
import textwrap
import statistics
import re
APP_VERSION = "v4.1.0-beta.1"

# Per-render computation cache.
# Streamlit reruns this module top-to-bottom, so these reset cleanly each rerun.
_RENDER_CACHE = {
    "plaid_paychecks": None,
    "historical_pay": None,
    "recurring_candidates": None,
}


st.set_page_config(
    page_title="Finance OS",
    page_icon="💠",
    layout="wide",
    initial_sidebar_state="collapsed",
)

LOCAL_STATE_FILE = Path(__file__).with_name("live_state.json")
DEFAULT_REPO = "dannyschweitzberger/finance-os"
DEFAULT_BRANCH = "main"
DEFAULT_STATE_PATH = "live_state.json"
DEFAULT_PLAID_VAULT_PATH = "plaid_item_vault.json"
DEFAULT_PLAID_REGISTRY_PATH = "plaid_item_registry.json"
DEFAULT_PRODUCTION_ITEM_GUARD = 3

# ---------- Standalone baseline ----------

def standalone_baseline():
    """
    Finance OS 4 no longer depends on an Excel workbook.

    These are neutral bootstrap values only. Real balances come from Plaid when
    available, while user preferences, paycheck overrides, goals, bills and
    planning state live in Finance OS persistent state.
    """
    now = datetime.today()
    return {
        "as_of": now,
        "score": 50,
        "free_cash": 0.0,
        "weather": "WATCH",
        "action": "BUILD BASELINE",
        "spend_change": 0.0,
        "projected_month_end": 0.0,
        "current_checking": 0.0,
        "current_savings": 0.0,
        "starting_checking": 0.0,
        "starting_savings": 0.0,
        "protected_buffer": 1000.0,
        "savings_rate": 0.25,
        "extra_savings_amount": 0.0,
        "extra_savings_date": None,
        "mtd_spend": 0.0,
        "spend_multiplier": 1.0,
        "forecast": [],
        "cards": [],
        "recurring_bills": [],
    }

BASE = standalone_baseline()

# ---------- Persistent live state ----------

def default_state():
    return {
        "version": 2,
        "transactions": [],
        "card_overrides": {
            c["card"]: {"balance": c["balance"], "limit": c["limit"]}
            for c in BASE["cards"] if c["card"]
        },
        "paychecks": {
            row["date"].date().isoformat(): {
                "expected": row["income"],
                "actual": None,
            }
            for row in BASE["forecast"]
        },
        "goals": [],
        "historical_pay": [],
        "plaid": {
            "items": [],
            "accounts": [],
            "transactions": [],
            "recurring": {"inflows": [], "outflows": []},
            "recurring_overrides": {},
            "income_source_overrides": {},
            "transaction_overrides": {},
            "reconciliation": {"last_run": None, "matched_quick_entries": []},
            "last_sync": None,
            "pending_link_token": None,
            "pending_hosted_url": None,
            "pending_update_item_id": None,
        },
        "settings": {
            "checking_adjustment": 0.0,
            "savings_adjustment": 0.0,
            "default_max_delay_days": 14,
            "smart_income_enabled": True,
            "visible_future_checks": 8,
            "forecast_engine_checks": 26,
            "auto_learn_from_plaid_payroll": True,
            "recurring_income_sources": {},
            "emergency_savings_floor": 1000.0,
            "preferred_savings_floor": 3000.0,
            "auto_bill_flex_enabled": True,
            "bill_flex_rules": {
                "Rent": {
                    "enabled": True,
                    "amount": 0.0,
                    "due_day": 1,
                    "policy": "next_paycheck",
                    "max_late_days": 14,
                    "fee": 0.0,
                },
                "Truck": {
                    "enabled": True,
                    "amount": 0.0,
                    "due_day": 15,
                    "policy": "under_30_days_late",
                    "max_late_days": 29,
                    "fee": 0.0,
                },
            },
        },
        "updated_at": None,
    }

def github_settings():
    try:
        cfg = st.secrets.get("github", {})
        token = cfg.get("token")
        repo = cfg.get("repo", DEFAULT_REPO)
        branch = cfg.get("branch", DEFAULT_BRANCH)
        state_path = cfg.get("state_path", DEFAULT_STATE_PATH)
        plaid_vault_path = cfg.get("plaid_vault_path", DEFAULT_PLAID_VAULT_PATH)
        plaid_registry_path = cfg.get("plaid_registry_path", DEFAULT_PLAID_REGISTRY_PATH)
        if token:
            return {
                "token": str(token),
                "repo": str(repo),
                "branch": str(branch),
                "state_path": str(state_path),
                "plaid_vault_path": str(plaid_vault_path),
                "plaid_registry_path": str(plaid_registry_path),
            }
    except Exception:
        pass
    return None

def github_request(url, method="GET", payload=None, token=None):
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Finance-OS-Streamlit",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read().decode("utf-8"))


def plaid_settings():
    """Read Plaid credentials from Streamlit secrets without exposing them to the UI."""
    try:
        cfg = st.secrets.get("plaid", {})
        client_id = cfg.get("client_id")
        secret = cfg.get("secret")
        environment = str(cfg.get("environment", "sandbox")).lower()
        if environment not in {"sandbox", "production"}:
            environment = "sandbox"
        if not client_id or not secret:
            return None
        return {
            "client_id": str(client_id),
            "secret": str(secret),
            "environment": environment,
            "redirect_uri": str(cfg.get("redirect_uri", "") or ""),
            "completion_redirect_uri": str(cfg.get("completion_redirect_uri", "") or ""),
            "token_encryption_key": str(cfg.get("token_encryption_key", "") or ""),
        }
    except Exception:
        return None


def plaid_crypto_ready():
    cfg = plaid_settings()
    return bool(cfg and cfg.get("token_encryption_key"))


def encrypt_plaid_access_token(token):
    if not token:
        return None
    cfg = plaid_settings()
    key = (cfg or {}).get("token_encryption_key")
    if not key:
        raise RuntimeError(
            "Plaid token encryption is not configured. Add token_encryption_key to Streamlit Secrets."
        )
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode("utf-8")).encrypt(
            str(token).encode("utf-8")
        ).decode("utf-8")
    except ImportError as exc:
        raise RuntimeError(
            "The cryptography package is required for encrypted Plaid token storage. "
            "Add cryptography>=46,<47 to requirements.txt."
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            "Could not encrypt the Plaid access token. Check token_encryption_key."
        ) from exc


def decrypt_plaid_access_token(item):
    if not item:
        return None

    encrypted = item.get("access_token_encrypted")
    if encrypted:
        cfg = plaid_settings()
        key = (cfg or {}).get("token_encryption_key")
        if not key:
            raise RuntimeError(
                "Plaid connection exists but token_encryption_key is missing from Streamlit Secrets."
            )
        try:
            from cryptography.fernet import Fernet
            return Fernet(key.encode("utf-8")).decrypt(
                encrypted.encode("utf-8")
            ).decode("utf-8")
        except ImportError as exc:
            raise RuntimeError(
                "The cryptography package is required to read the encrypted Plaid token."
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                "Could not decrypt this Plaid connection. The encryption key may have changed."
            ) from exc

    # v2.0.0 migration path: if an old plaintext token exists, use it once and
    # rewrite it encrypted the next time the state is saved.
    return item.get("access_token")


def secure_plaid_items_in_state(state):
    """
    Encrypt legacy plaintext Plaid tokens before serializing state.
    No access token is intentionally persisted in plaintext after v2.0.0.
    """
    items = state.get("plaid", {}).get("items", [])
    secured = []
    for item in items:
        row = dict(item)
        plaintext = row.pop("access_token", None)
        if plaintext and not row.get("access_token_encrypted"):
            row["access_token_encrypted"] = encrypt_plaid_access_token(plaintext)
        secured.append(row)
    state.get("plaid", {})["items"] = secured
    return state


def create_plaid_update_link(item):
    access_token = decrypt_plaid_access_token(item)
    if not access_token:
        raise RuntimeError("This Plaid connection has no usable access token.")

    cfg = plaid_settings()
    payload = {
        "client_name": "Finance OS",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": "finance-os-private-user"},
        "access_token": access_token,
        "hosted_link": {"url_lifetime_seconds": 1800},
    }
    if cfg and cfg.get("redirect_uri"):
        payload["redirect_uri"] = cfg["redirect_uri"]
    if cfg and cfg.get("completion_redirect_uri"):
        payload["hosted_link"]["completion_redirect_uri"] = cfg["completion_redirect_uri"]

    result = plaid_request("/link/token/create", payload)
    return result["link_token"], result["hosted_link_url"]


def plaid_item_status(item):
    access_token = decrypt_plaid_access_token(item)
    if not access_token:
        return {"healthy": False, "status": "Missing token", "error_code": "MISSING_TOKEN"}
    try:
        result = plaid_request("/item/get", {"access_token": access_token})
        item_info = result.get("item", {}) or {}
        error = item_info.get("error")
        if error:
            return {
                "healthy": False,
                "status": error.get("display_message") or error.get("error_message") or "Needs attention",
                "error_code": error.get("error_code"),
            }
        return {
            "healthy": True,
            "status": "Connected",
            "error_code": None,
            "institution_id": item_info.get("institution_id"),
        }
    except Exception as exc:
        return {"healthy": False, "status": str(exc), "error_code": None}


def disconnect_plaid_item(item):
    access_token = decrypt_plaid_access_token(item)
    if access_token:
        plaid_request("/item/remove", {"access_token": access_token})


class PlaidAPIError(RuntimeError):
    def __init__(self, message, code=None, error_type=None):
        super().__init__(message)
        self.code = code
        self.error_type = error_type


def plaid_request(endpoint, payload):
    cfg = plaid_settings()
    if not cfg:
        raise RuntimeError("Plaid credentials are not configured in Streamlit secrets.")

    body = {
        "client_id": cfg["client_id"],
        "secret": cfg["secret"],
        **payload,
    }
    url = f"https://{cfg['environment']}.plaid.com{endpoint}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Finance-OS-Streamlit/0.7.2",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = detail.get("error_message") or detail.get("display_message") or str(detail)
            code = detail.get("error_code")
            error_type = detail.get("error_type")
        except Exception:
            msg = str(exc)
            code = None
            error_type = None
        raise PlaidAPIError(msg, code=code, error_type=error_type) from exc


def create_plaid_hosted_link():
    cfg = plaid_settings()
    allowed, reason = production_new_item_allowed()
    if not allowed:
        raise RuntimeError(reason or "New Production Item creation is blocked by the safety guard.")
    payload = {
        "client_name": "Finance OS",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": "finance-os-private-user"},
        "products": ["transactions"],
        "transactions": {"days_requested": 730},
        "hosted_link": {"url_lifetime_seconds": 1800},
    }
    if cfg and cfg.get("redirect_uri"):
        payload["redirect_uri"] = cfg["redirect_uri"]
    if cfg and cfg.get("completion_redirect_uri"):
        payload["hosted_link"]["completion_redirect_uri"] = cfg["completion_redirect_uri"]

    result = plaid_request("/link/token/create", payload)
    return result["link_token"], result["hosted_link_url"]


def _find_public_token(obj):
    if isinstance(obj, dict):
        # Prefer newer results shape, but robustly handle any nested public token.
        for k, v in obj.items():
            if k == "public_token" and isinstance(v, str) and v.startswith("public-"):
                return v
            if k == "public_tokens" and isinstance(v, list):
                for x in v:
                    if isinstance(x, str) and x.startswith("public-"):
                        return x
        for v in obj.values():
            found = _find_public_token(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_public_token(v)
            if found:
                return found
    return None


def complete_plaid_hosted_link(link_token):
    result = plaid_request("/link/token/get", {"link_token": link_token})
    public_token = _find_public_token(result)
    if not public_token:
        return None
    exchanged = plaid_request(
        "/item/public_token/exchange",
        {"public_token": public_token},
    )
    access_token = exchanged.get("access_token")
    institution_id = None
    try:
        item_result = plaid_request("/item/get", {"access_token": access_token})
        institution_id = (item_result.get("item", {}) or {}).get("institution_id")
    except Exception:
        pass

    item = {
        "item_id": exchanged.get("item_id"),
        "access_token_encrypted": encrypt_plaid_access_token(access_token),
        "cursor": None,
        "connected_at": datetime.now().isoformat(timespec="seconds"),
        "needs_reauth": False,
        "institution_id": institution_id,
    }

    # Register immediately after exchange so the consumed Trial slot is never forgotten,
    # even if later syncing or app state persistence fails.
    register_plaid_item(item, environment=(plaid_settings() or {}).get("environment"))
    return item



def complete_plaid_update_link(link_token, existing_item):
    """
    Finish Plaid update mode without exchanging the public token.

    Plaid update mode preserves the existing Item access_token. We only use
    /link/token/get to confirm Hosted Link finished, then verify the existing Item.
    """
    if not existing_item:
        raise RuntimeError("The Plaid Item being repaired is no longer available.")

    result = plaid_request("/link/token/get", {"link_token": link_token})
    # Successful Hosted Link sessions expose an onSuccess public token/result,
    # but update mode explicitly does NOT exchange it.
    public_token = _find_public_token(result)
    if not public_token:
        return None

    access_token = decrypt_plaid_access_token(existing_item)
    if not access_token:
        raise RuntimeError("The existing Plaid Item has no usable access token.")

    verified = plaid_request("/item/get", {"access_token": access_token})
    item_info = verified.get("item", {}) or {}
    item_error = item_info.get("error")
    if item_error:
        raise RuntimeError(
            item_error.get("display_message")
            or item_error.get("error_message")
            or "Plaid still reports that this connection needs attention."
        )

    repaired = dict(existing_item)
    repaired["needs_reauth"] = False
    repaired.pop("reauth_error_code", None)
    repaired["reconnected_at"] = datetime.now().isoformat(timespec="seconds")
    repaired["institution_id"] = item_info.get(
        "institution_id",
        repaired.get("institution_id"),
    )
    register_plaid_item(
        repaired,
        environment=(plaid_settings() or {}).get("environment"),
    )
    return repaired

def plaid_sync_item(item):
    access_token = decrypt_plaid_access_token(item)
    if not access_token:
        return [], [], item.get("cursor"), []

    original_cursor = item.get("cursor")

    for restart_attempt in range(3):
        added_all = []
        modified_all = []
        removed_all = []
        cursor = original_cursor

        try:
            for _ in range(50):
                payload = {
                    "access_token": access_token,
                    "options": {"personal_finance_category_version": "v2"},
                }
                if cursor:
                    payload["cursor"] = cursor

                result = plaid_request("/transactions/sync", payload)
                added_all.extend(result.get("added", []))
                modified_all.extend(result.get("modified", []))
                removed_all.extend(result.get("removed", []))
                cursor = result.get("next_cursor", cursor)

                if not result.get("has_more"):
                    break

            accounts_result = plaid_request(
                "/accounts/balance/get",
                {"access_token": access_token},
            )
            return (
                added_all + modified_all,
                accounts_result.get("accounts", []),
                cursor,
                removed_all,
            )

        except PlaidAPIError as exc:
            if exc.code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" and restart_attempt < 2:
                continue
            if exc.code in {
                "ITEM_LOGIN_REQUIRED",
                "PENDING_EXPIRATION",
                "INVALID_CREDENTIALS",
                "USER_PERMISSION_REVOKED",
            }:
                item["needs_reauth"] = True
                item["reauth_error_code"] = exc.code
            raise

    raise RuntimeError("Plaid sync could not complete after retrying transaction pagination.")

def refresh_all_plaid_v3():
    """
    Refresh every existing Plaid Item without creating a new Item.
    Pulls transactions and /accounts/balance/get, merges by stable IDs, updates cursors,
    re-runs Quick Entry reconciliation, and returns a concise result summary.
    """
    plaid = STATE.setdefault("plaid", {})
    items = plaid.get("items", []) or []
    if not items:
        raise RuntimeError("No saved Plaid connections are available to refresh.")

    tx_by_id = {
        x.get("transaction_id"): dict(x)
        for x in plaid.get("transactions", []) or []
        if x.get("transaction_id")
    }
    account_by_id = {
        x.get("account_id"): dict(x)
        for x in plaid.get("accounts", []) or []
        if x.get("account_id")
    }

    refreshed = 0
    errors = []
    for item in items:
        try:
            changed, accounts, cursor, removed = plaid_sync_item(item)

            for rem in removed or []:
                rid = rem.get("transaction_id") if isinstance(rem, dict) else None
                if rid:
                    tx_by_id.pop(rid, None)

            for raw_tx in changed or []:
                ntx = normalize_plaid_transaction(raw_tx)
                tid = ntx.get("transaction_id")
                if tid:
                    tx_by_id[tid] = ntx

            for raw_acct in accounts or []:
                nacct = normalize_plaid_account(raw_acct)
                aid = nacct.get("account_id")
                if aid:
                    account_by_id[aid] = nacct

            item["cursor"] = cursor
            item["last_sync"] = datetime.now().isoformat(timespec="seconds")
            item["needs_reauth"] = False
            item.pop("reauth_error_code", None)
            refreshed += 1
        except Exception as exc:
            errors.append(str(exc))

    if refreshed <= 0:
        plaid["last_sync_error"] = " | ".join(errors) if errors else "Plaid refresh failed."
        raise RuntimeError(plaid["last_sync_error"])

    plaid["transactions"] = sorted(
        tx_by_id.values(),
        key=lambda x: (str(x.get("date") or ""), str(x.get("transaction_id") or "")),
    )
    plaid["accounts"] = list(account_by_id.values())
    plaid["last_sync"] = datetime.now().isoformat(timespec="seconds")
    plaid["last_sync_error"] = " | ".join(errors) if errors else None

    # v3 always treats a successfully synced/mapped Plaid source as active.
    plaid["use_live_balances"] = True
    reconcile_quick_entries_with_plaid()

    # Flush per-render derived caches so payroll/recurring-income detection re-runs.
    try:
        _RENDER_CACHE["plaid_paychecks"] = None
        _RENDER_CACHE["recurring_candidates"] = None
    except Exception:
        pass

    return {
        "connections_refreshed": refreshed,
        "connections_failed": len(errors),
        "transactions": len(plaid.get("transactions", []) or []),
        "accounts": len(plaid.get("accounts", []) or []),
        "last_sync": plaid["last_sync"],
        "errors": errors,
    }


def normalize_plaid_transaction(tx):
    pfc = tx.get("personal_finance_category") or {}
    amount = float(tx.get("amount", 0) or 0)
    counterparties = tx.get("counterparties") or []
    return {
        "transaction_id": tx.get("transaction_id"),
        "account_id": tx.get("account_id"),
        "date": tx.get("date"),
        "authorized_date": tx.get("authorized_date"),
        "name": tx.get("name") or "",
        "merchant_name": tx.get("merchant_name") or "",
        "amount": amount,
        "pending": bool(tx.get("pending", False)),
        "category_primary": pfc.get("primary") or "",
        "category_detailed": pfc.get("detailed") or "",
        "category_confidence": pfc.get("confidence_level") or "",
        "payment_channel": tx.get("payment_channel") or "",
        "website": tx.get("website") or "",
        "counterparties": counterparties,
    }


def normalize_plaid_account(a):
    balances = a.get("balances") or {}
    return {
        "account_id": a.get("account_id"),
        "name": a.get("name") or "",
        "official_name": a.get("official_name") or "",
        "type": a.get("type") or "",
        "subtype": a.get("subtype") or "",
        "mask": a.get("mask") or "",
        "current": balances.get("current"),
        "available": balances.get("available"),
        "currency": balances.get("iso_currency_code") or "USD",
    }


def _tx_desc(tx):
    return " ".join(
        str(x or "").strip()
        for x in [tx.get("merchant_name"), tx.get("name")]
        if str(x or "").strip()
    ).strip()


def _normalized_source(tx):
    """
    Normalize a Plaid merchant/transaction description into a stable source key.

    This must never raise: messy bank strings are expected.
    """
    try:
        raw = str(
            tx.get("merchant_name")
            or tx.get("name")
            or tx.get("original_description")
            or ""
        ).upper()

        # Remove common noisy identifiers and excess punctuation.
        raw = re.sub(r"\bID\s*\d+\b", " ", raw)
        raw = re.sub(r"\b\d{4,}\b", " ", raw)
        raw = re.sub(r"[^A-Z0-9 ]+", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip()

        return raw[:120] if raw else "UNKNOWN"
    except Exception:
        return "UNKNOWN"



def classify_plaid_transaction(tx):
    """
    Finance OS transaction-intelligence layer.

    Plaid uses positive amounts for money leaving an account and negative amounts
    for deposits/credits. The classification is intentionally conservative:
    transfers, debt payments, refunds/credits, interest, tax-related deposits,
    gambling cash-outs, and reimbursements are not treated as payroll or true spend.
    """
    try:
        amount = float(tx.get("amount", 0) or 0)
        primary = str(tx.get("category_primary", "") or "").upper()
        detailed = str(tx.get("category_detailed", "") or "").upper()
        desc = _tx_desc(tx).upper()
        source = _normalized_source(tx)
        account_id = tx.get("account_id")

        overrides = STATE.get("plaid", {}).get("transaction_overrides", {})
        override = overrides.get(str(tx.get("transaction_id") or ""))
        if override:
            return override

        transfer_terms = (
            "TRANSFER", "ACH TRANSFER", "ZELLE", "VENMO TRANSFER", "CASH APP TRANSFER",
            "ONLINE TRANSFER", "MOBILE TRANSFER", "INTERNAL TRANSFER",
        )
        card_payment_terms = (
            "CREDIT CARD PAYMENT", "CARD PAYMENT", "PAYMENT THANK YOU",
            "AUTOPAY PAYMENT", "ONLINE PAYMENT", "MOBILE PAYMENT",
        )
        refund_terms = (
            "REFUND", "RETURN", "REVERSAL", "REIMBURSE", "STATEMENT CREDIT",
            "BENEFIT CRDT", "BENEFIT CREDIT", "CASHBACK", "CASH BACK",
        )
        payroll_terms = ("PAYROLL", "PAYCHECK", "SALARY", "WAGES", "DIRECT DEP")
        interest_terms = ("INTEREST PAYMENT", "INTEREST PAID", "APY INTEREST")
        tax_terms = ("TURBOTAX", "IRS", "TAX REFUND", "STATE TAX", "TREASURY TAX")
        gambling_credit_terms = (
            "CASINO VISA DIRECT", "CASINO", "SPORTSBOOK", "BETMGM", "DRAFTKINGS",
            "FANDUEL", "CAESARS SPORTSBOOK",
        )

        # Explicit Plaid categories first.
        if "CREDIT_CARD_PAYMENT" in detailed or any(t in desc for t in card_payment_terms):
            return "credit_card_payment"
        if (
            primary in {"TRANSFER_IN", "TRANSFER_OUT"}
            or "TRANSFER" in detailed
            or any(t in desc for t in transfer_terms)
        ):
            return "internal_or_external_transfer"
        if "LOAN_PAYMENT" in detailed:
            return "debt_payment"

        # Deposits / credits.
        if amount < 0:
            if any(t in desc for t in interest_terms):
                return "interest_income"
            if any(t in desc for t in tax_terms):
                return "tax_refund_or_credit"
            if any(t in desc for t in gambling_credit_terms):
                return "gambling_proceeds"
            if any(t in desc for t in refund_terms):
                return "refund_or_reimbursement"

            payroll_override = STATE.get("plaid", {}).get("income_source_overrides", {}).get(source)
            if payroll_override == "payroll":
                return "payroll"
            if payroll_override == "other_income":
                return "other_income"
            if payroll_override == "ignore":
                return "deposit_other"

            strict_payroll = (
                "WAGE" in detailed
                or "PAYROLL" in detailed
                or any(t in desc for t in payroll_terms)
                or "ADP" in desc
            )
            if strict_payroll:
                return "payroll"

            # Generic INCOME is not enough to call something payroll.
            if primary == "INCOME":
                return "other_income"
            return "deposit_other"

        # Outflows.
        if any(t in desc for t in refund_terms):
            return "adjustment"
        if "INTEREST_CHARGE" in detailed or "INTEREST CHARGE" in desc:
            return "interest_fee"
        if "FEE" in detailed or "LATE FEE" in desc or "ATM FEE" in desc:
            return "fee"
        return "true_spending"

    except Exception:
        return "other"


def income_source_intelligence():
    """
    Groups deposits by source and identifies plausible recurring payroll sources.
    A source needs payroll semantics or a strong repeated cadence; generic deposits
    do not become payroll merely because Plaid categorizes them as income.
    """
    groups = {}
    for tx in STATE.get("plaid", {}).get("transactions", []):
        if tx.get("pending"):
            continue
        amount = float(tx.get("amount", 0) or 0)
        if amount >= 0:
            continue
        kind = classify_plaid_transaction(tx)
        source = _normalized_source(tx)
        try:
            d = datetime.fromisoformat(str(tx.get("date"))).date()
        except Exception:
            continue
        groups.setdefault(source, []).append({
            "date": d,
            "net": abs(amount),
            "description": tx.get("merchant_name") or tx.get("name") or source,
            "transaction_id": tx.get("transaction_id"),
            "kind": kind,
        })

    rows = []
    for source, items in groups.items():
        items = sorted(items, key=lambda x: x["date"])
        payroll_items = [x for x in items if x["kind"] == "payroll"]
        if payroll_items:
            use = payroll_items
            classification = "Payroll"
            confidence = "High"
        else:
            use = items
            classification = "Other deposits"
            confidence = "Low"

        amounts = [x["net"] for x in use]
        gaps = [(use[i]["date"]-use[i-1]["date"]).days for i in range(1,len(use))]
        biweekly = sum(1 for g in gaps if 12 <= g <= 16)
        semi_monthly = sum(1 for g in gaps if 13 <= g <= 18)
        recurring_score = max(biweekly, semi_monthly) / max(1, len(gaps)) if gaps else 0.0

        if classification != "Payroll" and len(use) >= 4 and recurring_score >= .70:
            # Still only a candidate; user should approve it before payroll import.
            classification = "Payroll candidate"
            confidence = "Medium"

        rows.append({
            "Source": source.title(),
            "Classification": classification,
            "Confidence": confidence,
            "Deposits": len(items),
            "Average": sum(amounts)/len(amounts) if amounts else 0.0,
            "Recent": items[-1]["date"],
            "_key": source,
        })
    return sorted(rows, key=lambda x: (
        {"Payroll":0,"Payroll candidate":1,"Other deposits":2}.get(x["Classification"],9),
        -x["Deposits"],
    ))


def plaid_detected_paychecks():

    if _RENDER_CACHE["plaid_paychecks"] is not None:
        return _RENDER_CACHE["plaid_paychecks"]
    rows = []
    for tx in STATE.get("plaid", {}).get("transactions", []):
        if tx.get("pending"):
            continue
        if classify_plaid_transaction(tx) != "payroll":
            continue

        amount = float(tx.get("amount", 0) or 0)
        if amount >= 0:
            continue
        try:
            d = datetime.fromisoformat(str(tx.get("date"))).date()
        except Exception:
            continue
        rows.append({
            "date": d,
            "net": abs(amount),
            "description": tx.get("merchant_name") or tx.get("name") or "Payroll",
            "source": _normalized_source(tx),
            "transaction_id": tx.get("transaction_id"),
        })
    _RENDER_CACHE["plaid_paychecks"] = sorted(rows, key=lambda x: x["date"])
    return _RENDER_CACHE["plaid_paychecks"]


def transaction_intelligence_summary(days=730):
    cutoff = BASE["as_of"].date() - timedelta(days=int(days))
    counts = {}
    amounts = {}
    for tx in STATE.get("plaid", {}).get("transactions", []):
        if tx.get("pending"):
            continue
        try:
            d = datetime.fromisoformat(str(tx.get("date"))).date()
        except Exception:
            continue
        if d < cutoff:
            continue
        kind = classify_plaid_transaction(tx)
        counts[kind] = counts.get(kind, 0) + 1
        amounts[kind] = amounts.get(kind, 0.0) + abs(float(tx.get("amount",0) or 0))
    return {"counts": counts, "amounts": amounts}


def _income_source_key(tx):
    return _normalized_source(tx)


def recurring_nonpayroll_income_candidates():
    """
    Detect recurring deposits that are not payroll. Nothing is forecast until
    the user confirms the source as dependable recurring income.
    """
    if _RENDER_CACHE["recurring_candidates"] is not None:
        return _RENDER_CACHE["recurring_candidates"]

    groups = {}
    for tx in STATE.get("plaid", {}).get("transactions", []):
        if tx.get("pending"):
            continue
        amount = float(tx.get("amount", 0) or 0)
        if amount >= 0:
            continue

        kind = classify_plaid_transaction(tx)
        if kind in {
            "payroll", "tax_refund_or_credit", "gambling_proceeds",
            "interest_income", "refund_or_reimbursement",
            "internal_or_external_transfer",
        }:
            continue

        try:
            d = datetime.fromisoformat(str(tx.get("date"))).date()
        except Exception:
            continue

        key = _income_source_key(tx)
        groups.setdefault(key, []).append({
            "date": d,
            "amount": abs(amount),
            "description": tx.get("merchant_name") or tx.get("name") or key,
            "transaction_id": tx.get("transaction_id"),
        })

    results = []
    overrides = STATE["settings"].get("recurring_income_sources", {})

    for key, items in groups.items():
        items = sorted(items, key=lambda x: x["date"])
        if len(items) < 2:
            continue

        gaps = [(items[i]["date"] - items[i-1]["date"]).days for i in range(1, len(items))]
        monthly_hits = sum(1 for g in gaps if 24 <= g <= 38)
        monthly_conf = monthly_hits / max(1, len(gaps))

        amounts = [x["amount"] for x in items]
        mean_amt = sum(amounts) / len(amounts)
        cv = (
            (sum((a-mean_amt)**2 for a in amounts) / len(amounts)) ** .5 / mean_amt
            if mean_amt else 1.0
        )
        amount_consistency = max(0.0, min(1.0, 1.0-cv))

        saved = overrides.get(key, {})
        confirmed = bool(saved.get("confirmed", False))

        results.append({
            "key": key,
            "Source": saved.get("name") or items[-1]["description"],
            "Typical amount": float(saved.get("amount", mean_amt) or mean_amt),
            "Occurrences": len(items),
            "Last seen": items[-1]["date"],
            "Monthly confidence": monthly_conf,
            "Amount consistency": amount_consistency,
            "Confirmed": confirmed,
            "Category": saved.get("category", "Recurring household income"),
            "Day target": int(saved.get("day_target", round(sum(x["date"].day for x in items)/len(items)))),
        })

    _RENDER_CACHE["recurring_candidates"] = sorted(
        results,
        key=lambda x: (not x["Confirmed"], -x["Monthly confidence"], -x["Occurrences"])
    )
    return _RENDER_CACHE["recurring_candidates"]


def confirmed_recurring_income_sources():
    return [x for x in recurring_nonpayroll_income_candidates() if x["Confirmed"]]


def expected_recurring_income_between(start_date, end_date):
    """
    Return expected recurring household-income deposits inside a date window.
    These are forecast dollars only until Plaid confirms the actual deposit.
    """
    expected = []
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()

    for src in confirmed_recurring_income_sources():
        day = max(1, min(28, int(src.get("Day target", 1) or 1)))
        cursor = date(start_date.year, start_date.month, 1)
        while cursor <= end_date:
            try:
                d = date(cursor.year, cursor.month, day)
            except ValueError:
                d = date(cursor.year, cursor.month, 28)

            if start_date <= d <= end_date:
                # If an actual matching deposit already posted near this expected date,
                # use actual and mark confirmed instead of double-counting expected.
                actual = None
                for tx in STATE.get("plaid", {}).get("transactions", []):
                    if tx.get("pending"):
                        continue
                    amt = float(tx.get("amount", 0) or 0)
                    if amt >= 0:
                        continue
                    if _income_source_key(tx) != src["key"]:
                        continue
                    try:
                        td = datetime.fromisoformat(str(tx.get("date"))).date()
                    except Exception:
                        continue
                    if abs((td-d).days) <= 5:
                        actual = abs(amt)
                        break

                expected.append({
                    "date": d,
                    "source": src["Source"],
                    "amount": float(actual if actual is not None else src["Typical amount"]),
                    "status": "Confirmed" if actual is not None else "Expected",
                    "key": src["key"],
                })

            # next month
            y = cursor.year + (1 if cursor.month == 12 else 0)
            m = 1 if cursor.month == 12 else cursor.month + 1
            cursor = date(y, m, 1)

    return expected



def plaid_spend_rows(days=730):
    cutoff = BASE["as_of"].date() - timedelta(days=int(days))
    rows = []
    for tx in STATE.get("plaid", {}).get("transactions", []):
        if tx.get("pending"):
            continue
        try:
            d = datetime.fromisoformat(str(tx.get("date"))).date()
        except Exception:
            continue
        if d < cutoff:
            continue

        amount = float(tx.get("amount", 0) or 0)
        if amount <= 0:
            continue

        kind = classify_plaid_transaction(tx)
        if kind != "true_spending":
            continue

        primary = str(tx.get("category_primary", "")).upper()
        detailed = str(tx.get("category_detailed", "")).upper()
        rows.append({
            "date": d,
            "amount": amount,
            "primary": primary or "OTHER",
            "detailed": detailed,
            "merchant": tx.get("merchant_name") or tx.get("name") or "Unknown",
            "transaction_id": tx.get("transaction_id"),
        })
    return rows


def spending_intelligence():
    rows = plaid_spend_rows(730)
    if not rows:
        return None

    as_of = BASE["as_of"].date()
    recent_cut = as_of - timedelta(days=90)
    prior_cut = as_of - timedelta(days=365)

    recent = [r for r in rows if r["date"] >= recent_cut]
    prior = [r for r in rows if prior_cut <= r["date"] < recent_cut]
    full = rows

    def monthly_rate(items, days):
        return sum(x["amount"] for x in items) / max(1.0, days / 30.4375)

    recent_monthly = monthly_rate(recent, 90)
    prior_monthly = monthly_rate(prior, 275) if prior else 0.0
    full_days = max(30, (as_of - min(r["date"] for r in full)).days + 1)
    full_monthly = monthly_rate(full, full_days)

    cats = {}
    for r in full:
        bucket = cats.setdefault(r["primary"], {"recent":0.0,"prior":0.0,"full":0.0})
        bucket["full"] += r["amount"]
        if r["date"] >= recent_cut:
            bucket["recent"] += r["amount"]
        elif r["date"] >= prior_cut:
            bucket["prior"] += r["amount"]

    category_rows = []
    for cat, vals in cats.items():
        recent_rate = vals["recent"] / 3.0
        prior_rate = vals["prior"] / (275 / 30.4375) if prior else 0.0
        category_rows.append({
            "Category": cat.replace("_"," ").title(),
            "Last 90d / mo": recent_rate,
            "Prior period / mo": prior_rate,
            "Change / mo": recent_rate-prior_rate,
        })
    category_rows.sort(key=lambda x: abs(x["Change / mo"]), reverse=True)

    excluded_summary = transaction_intelligence_summary(365)
    excluded_types = {
        k: {"count": excluded_summary["counts"].get(k,0),
            "amount": excluded_summary["amounts"].get(k,0.0)}
        for k in [
            "credit_card_payment","internal_or_external_transfer","debt_payment",
            "refund_or_reimbursement","adjustment","interest_fee","fee"
        ]
    }

    return {
        "recent_monthly": recent_monthly,
        "prior_monthly": prior_monthly,
        "full_monthly": full_monthly,
        "categories": category_rows,
        "transactions": rows,
        "excluded": excluded_types,
    }


def _merchant_key(row):
    """Canonical merchant identity for recurring-pattern analysis."""
    raw = (row.get("merchant") or row.get("name") or "").strip().upper()
    compact = re.sub(r"[^A-Z0-9]+", " ", raw).strip()

    # Known merchant families: different statement descriptors, same obligation.
    if "XFINITY" in compact or "COMCAST" in compact:
        return "XFINITY / COMCAST"
    if "SPOTIFY" in compact:
        return "SPOTIFY"

    return compact or raw


def _cadence_from_gaps(gaps):
    if not gaps:
        return None, None, 0.0
    ordered = sorted(gaps)
    med = ordered[len(ordered)//2]

    if 26 <= med <= 36:
        target, cadence, factor = 30.4, "Monthly", 1.0
    elif 13 <= med <= 16:
        target, cadence, factor = 14.0, "Biweekly", 26/12
    elif 6 <= med <= 8:
        target, cadence, factor = 7.0, "Weekly", 52/12
    elif 80 <= med <= 100:
        target, cadence, factor = 91.0, "Quarterly", 4/12
    elif 350 <= med <= 380:
        target, cadence, factor = 365.0, "Annual", 1/12
    else:
        return None, None, 0.0

    consistency = sum(1 for g in gaps if abs(g-target) <= max(3, target*.16)) / len(gaps)
    return cadence, factor, consistency


def classify_repeat_spending():
    """
    Separates true recurring obligations from merely repeated merchants.
    Nothing inferred here automatically becomes a modeled bill.
    """
    txs = plaid_spend_rows(730)
    groups = {}
    for row in txs:
        key = _merchant_key(row)
        if not key or key == "UNKNOWN":
            continue
        groups.setdefault(key, []).append(row)

    overrides = STATE.get("plaid", {}).get("recurring_overrides", {})
    results = []

    subscription_words = {
        "NETFLIX","SPOTIFY","HULU","MAX","DISNEY","APPLE.COM/BILL","ICLOUD",
        "YOUTUBE","AMAZON PRIME","MICROSOFT","ADOBE","GYM","FITNESS",
        "INSURANCE","INTERNET","WIRELESS","PHONE","UTILITY","UTILITIES",
        "ELECTRIC","ENERGY","WATER","RENT","MORTGAGE","LOAN"
    }
    discretionary_categories = {
        "FOOD_AND_DRINK","TRANSPORTATION","GENERAL_MERCHANDISE",
        "ENTERTAINMENT","TRAVEL"
    }

    for merchant, items in groups.items():
        items = sorted(items, key=lambda x: x["date"])
        if len(items) < 2:
            continue

        amounts = [float(x["amount"]) for x in items]
        avg_amt = sum(amounts) / len(amounts)
        if avg_amt <= 0:
            continue

        gaps = [
            (items[i]["date"] - items[i-1]["date"]).days
            for i in range(1, len(items))
        ]
        cadence, monthly_factor, cadence_consistency = _cadence_from_gaps(gaps)

        # Amount consistency: fixed subscriptions score much higher than variable merchants.
        mean_amt = avg_amt
        variance = sum((a-mean_amt)**2 for a in amounts) / max(1, len(amounts))
        sd = variance ** 0.5
        cv = sd / mean_amt if mean_amt else 1.0
        amount_consistency = max(0.0, min(1.0, 1.0 - cv))

        primary = str(items[-1].get("primary","")).upper()
        detailed = str(items[-1].get("detailed","")).upper()
        merchant_upper = merchant.upper()

        # Fees and interest are financial leakage, not recurring household obligations.
        if (
            "INTEREST" in merchant_upper
            or "INTEREST" in detailed
            or "FEE" in merchant_upper
            or "FEE" in detailed
        ):
            results.append({
                "Merchant": merchant.title(),
                "Classification": "Fee / interest",
                "Confidence": "High",
                "Score": 100,
                "Cadence": cadence or "Irregular",
                "Typical amount": avg_amt,
                "Monthly equivalent": avg_amt * monthly_factor if monthly_factor else 0.0,
                "Occurrences": len(items),
                "Last seen": items[-1]["date"],
                "Category": primary.replace("_"," ").title(),
                "_key": merchant_upper,
            })
            continue

        semantic_hint = any(w in merchant_upper or w in detailed for w in subscription_words)
        discretionary = primary in discretionary_categories

        score = 0.0
        if cadence:
            score += cadence_consistency * 45
        if len(items) >= 3:
            score += min(20, (len(items)-2)*5)
        score += amount_consistency * 20
        if semantic_hint:
            score += 25
        if discretionary and not semantic_hint:
            score -= 25
        if cv > .45:
            score -= 15

        override = overrides.get(merchant_upper)
        if override in {"bill","subscription","repeat","irregular"}:
            classification = {
                "bill":"Confirmed bill",
                "subscription":"Confirmed subscription",
                "repeat":"Repeat merchant",
                "irregular":"Repeat merchant",
            }[override]
            confidence = "Confirmed" if override != "irregular" else "User set"
            # A user-confirmed bill/subscription is recurring even when the historical
            # spacing is noisy. Preserve the observed cadence as supporting evidence,
            # but never let "Irregular" undo the user's explicit classification.
            if override in {"bill","subscription"} and not cadence:
                cadence = "User confirmed"
                monthly_factor = 1.0
        else:
            if score >= 70 and cadence and not (discretionary and not semantic_hint):
                classification = "Likely recurring charge"
                confidence = "High"
            elif score >= 48 and cadence:
                classification = "Possible recurring charge"
                confidence = "Medium"
            else:
                classification = "Repeat merchant"
                confidence = "Low"

        results.append({
            "Merchant": merchant.title(),
            "Classification": classification,
            "Confidence": confidence,
            "Score": round(max(0, min(100, score))),
            "Cadence": cadence or "Irregular",
            "Typical amount": avg_amt,
            "Monthly equivalent": avg_amt * monthly_factor if monthly_factor else 0.0,
            "Occurrences": len(items),
            "Last seen": items[-1]["date"],
            "Category": primary.replace("_"," ").title(),
            "_key": merchant_upper,
        })

    rank = {
        "Confirmed bill": 0,
        "Confirmed subscription": 0,
        "Likely recurring charge": 1,
        "Possible recurring charge": 2,
        "Repeat merchant": 3,
        "Fee / interest": 4,
    }
    return sorted(
        results,
        key=lambda x: (rank.get(x["Classification"], 9), -x["Score"], -x["Typical amount"])
    )


def inferred_recurring_outflows():
    """
    Compatibility helper used elsewhere in Finance OS.
    Only returns recurring-looking obligations; repeat merchants are intentionally excluded.
    """
    return [
        x for x in classify_repeat_spending()
        if x["Classification"] in {
            "Confirmed bill",
            "Confirmed subscription",
            "Likely recurring charge",
        }
    ]


def _github_read_json_file(cfg, path):
    """Read a JSON file from the configured private GitHub repo."""
    url = (
        f"https://api.github.com/repos/{cfg['repo']}/contents/"
        f"{path}?ref={cfg['branch']}"
    )
    result = github_request(url, token=cfg["token"])
    return json.loads(base64.b64decode(result["content"]).decode("utf-8"))


def _github_write_json_file(cfg, path, payload, message):
    """Create/update a JSON file in the configured private GitHub repo."""
    get_url = (
        f"https://api.github.com/repos/{cfg['repo']}/contents/"
        f"{path}?ref={cfg['branch']}"
    )
    sha = None
    try:
        existing = github_request(get_url, token=cfg["token"])
        sha = existing.get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    put_url = (
        f"https://api.github.com/repos/{cfg['repo']}/contents/"
        f"{path}"
    )
    body = json.dumps(payload, indent=2, ensure_ascii=False)
    request_payload = {
        "message": message,
        "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "branch": cfg["branch"],
    }
    if sha:
        request_payload["sha"] = sha
    github_request(
        put_url,
        method="PUT",
        payload=request_payload,
        token=cfg["token"],
    )


def plaid_vault_payload(state):
    """
    Small independent recovery copy of Plaid Items.

    Access tokens are already Fernet-encrypted before they reach this payload.
    Keeping this separate from live_state.json prevents an unrelated state reset
    from orphaning Production Items.
    """
    clean = clean_state_for_save(state)
    plaid = clean.get("plaid", {}) or {}
    return {
        "version": 1,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "items": plaid.get("items", []) or [],
        "account_map": plaid.get(
            "account_map",
            {"checking": None, "savings": None, "cards": {}},
        ),
    }


def save_plaid_vault(state):
    cfg = github_settings()
    items = (state.get("plaid", {}) or {}).get("items", []) or []
    if not cfg or not items:
        return False
    _github_write_json_file(
        cfg,
        cfg.get("plaid_vault_path", DEFAULT_PLAID_VAULT_PATH),
        plaid_vault_payload(state),
        "Finance OS: protect Plaid Item vault",
    )
    return True


def restore_plaid_vault_if_needed(state):
    """
    Restore encrypted Plaid Item records when the main state unexpectedly has none.

    We never overwrite a non-empty current Item list. The vault is recovery-only.
    """
    cfg = github_settings()
    if not cfg:
        return state

    plaid = state.setdefault("plaid", {})
    if plaid.get("items"):
        return state

    try:
        vault = _github_read_json_file(
            cfg,
            cfg.get("plaid_vault_path", DEFAULT_PLAID_VAULT_PATH),
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return state
        raise
    except Exception:
        return state

    vault_items = vault.get("items", []) or []
    if not vault_items:
        return state

    plaid["items"] = vault_items
    if not plaid.get("account_map"):
        plaid["account_map"] = vault.get(
            "account_map",
            {"checking": None, "savings": None, "cards": {}},
        )
    plaid["vault_restored_at"] = datetime.now().isoformat(timespec="seconds")
    return state


def load_plaid_item_registry():
    """
    Read the append-only registry of Production Item IDs Finance OS has ever created.

    Unlike the active Item list, entries are intentionally retained after disconnect because
    Plaid Trial slots are not returned by /item/remove.
    """
    cfg = github_settings()
    if not cfg:
        return {"version": 1, "items": []}
    try:
        payload = _github_read_json_file(
            cfg,
            cfg.get("plaid_registry_path", DEFAULT_PLAID_REGISTRY_PATH),
        )
        if not isinstance(payload, dict):
            return {"version": 1, "items": []}
        payload.setdefault("version", 1)
        payload.setdefault("items", [])
        return payload
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"version": 1, "items": []}
        raise
    except Exception:
        return {"version": 1, "items": []}


def register_plaid_item(item, environment=None):
    """
    Append one Plaid Item to the durable registry.

    Existing item_ids are updated, never duplicated. Removed/inactive Items remain in this
    registry because they still consumed a Trial slot.
    """
    if not item or not item.get("item_id"):
        return False

    cfg = github_settings()
    if not cfg:
        return False

    registry = load_plaid_item_registry()
    rows = registry.get("items", []) or []
    by_id = {
        str(row.get("item_id")): dict(row)
        for row in rows
        if row.get("item_id")
    }

    item_id = str(item.get("item_id"))
    previous = by_id.get(item_id, {})
    by_id[item_id] = {
        **previous,
        "item_id": item_id,
        "institution_id": item.get("institution_id") or previous.get("institution_id"),
        "environment": environment or previous.get("environment") or (plaid_settings() or {}).get("environment"),
        "first_seen_at": previous.get("first_seen_at") or datetime.now().isoformat(timespec="seconds"),
        "last_seen_at": datetime.now().isoformat(timespec="seconds"),
        "active": True,
    }

    payload = {
        "version": 1,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "items": sorted(by_id.values(), key=lambda x: str(x.get("first_seen_at", ""))),
    }
    _github_write_json_file(
        cfg,
        cfg.get("plaid_registry_path", DEFAULT_PLAID_REGISTRY_PATH),
        payload,
        "Finance OS: register Plaid Production Item",
    )
    return True


def mark_plaid_item_inactive(item_id):
    """Mark an Item inactive without erasing its consumed Trial-slot record."""
    if not item_id:
        return False
    cfg = github_settings()
    if not cfg:
        return False
    registry = load_plaid_item_registry()
    changed = False
    for row in registry.get("items", []) or []:
        if str(row.get("item_id")) == str(item_id):
            row["active"] = False
            row["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
            changed = True
            break
    if changed:
        registry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _github_write_json_file(
            cfg,
            cfg.get("plaid_registry_path", DEFAULT_PLAID_REGISTRY_PATH),
            registry,
            "Finance OS: mark Plaid Item inactive",
        )
    return changed


def plaid_registry_summary():
    registry = load_plaid_item_registry()
    rows = registry.get("items", []) or []
    production_rows = [
        x for x in rows
        if str(x.get("environment", "production")).lower() == "production"
    ]
    active_rows = [x for x in production_rows if x.get("active", True)]
    return {
        "total_known_production_items": len(production_rows),
        "active_known_production_items": len(active_rows),
        "rows": production_rows,
    }


def production_new_item_allowed():
    """
    Hard safety gate for Trial usage.

    Finance OS is a personal app expected to use three institutions. Once three Production
    Item IDs have ever been registered, new-item Link is blocked. Existing Items remain
    reconnectable via update mode.
    """
    cfg = plaid_settings()
    if not cfg or cfg.get("environment") != "production":
        return True, None

    if not github_settings():
        return False, (
            "New Production connections are blocked until durable GitHub storage is configured."
        )

    summary = plaid_registry_summary()
    used = summary["total_known_production_items"]
    if used >= DEFAULT_PRODUCTION_ITEM_GUARD:
        return False, (
            f"New Production Item creation is locked: Finance OS already knows about "
            f"{used} Production Item(s). Use Reconnect/update mode for existing institutions."
        )
    return True, None

def load_state():
    cfg = github_settings()
    if cfg:
        url = (
            f"https://api.github.com/repos/{cfg['repo']}/contents/"
            f"{cfg['state_path']}?ref={cfg['branch']}"
        )
        try:
            result = github_request(url, token=cfg["token"])
            content = base64.b64decode(result["content"]).decode("utf-8")
            state = json.loads(content)
            state = restore_plaid_vault_if_needed(state)
            state["_storage"] = "github"
            return state
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                st.warning("Live state could not be read from GitHub. Using local fallback.")
        except Exception:
            st.warning("Live state could not be read from GitHub. Using local fallback.")

    if LOCAL_STATE_FILE.exists():
        try:
            state = json.loads(LOCAL_STATE_FILE.read_text(encoding="utf-8"))
            state = restore_plaid_vault_if_needed(state)
            state["_storage"] = "local"
            return state
        except Exception:
            pass

    state = default_state()
    state = restore_plaid_vault_if_needed(state)
    state["_storage"] = "memory"
    return state

def clean_state_for_save(state):
    # Copy through JSON so token migration does not mutate unrelated session objects.
    clean = {k: v for k, v in state.items() if not k.startswith("_")}
    clean = json.loads(json.dumps(clean))
    if clean.get("plaid", {}).get("items"):
        secure_plaid_items_in_state(clean)
    return clean

def save_state(state, message="Update Finance OS live state"):
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    clean = clean_state_for_save(state)
    content_text = json.dumps(clean, indent=2, ensure_ascii=False)

    cfg = github_settings()
    if cfg:
        get_url = (
            f"https://api.github.com/repos/{cfg['repo']}/contents/"
            f"{cfg['state_path']}?ref={cfg['branch']}"
        )
        sha = None
        try:
            existing = github_request(get_url, token=cfg["token"])
            sha = existing.get("sha")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        put_url = (
            f"https://api.github.com/repos/{cfg['repo']}/contents/"
            f"{cfg['state_path']}"
        )
        payload = {
            "message": message,
            "content": base64.b64encode(content_text.encode("utf-8")).decode("ascii"),
            "branch": cfg["branch"],
        }
        if sha:
            payload["sha"] = sha
        github_request(put_url, method="PUT", payload=payload, token=cfg["token"])
        # Keep an independent encrypted recovery copy of Plaid Item records.
        if clean.get("plaid", {}).get("items"):
            try:
                save_plaid_vault(clean)
            except Exception:
                # Never fail the user's main state save solely because the recovery
                # vault could not be refreshed; Accounts page surfaces vault status.
                pass
        st.cache_data.clear()
        return "github"

    # Functional fallback, but Streamlit Cloud may lose this after a restart/redeploy.
    LOCAL_STATE_FILE.write_text(content_text, encoding="utf-8")
    return "local"

if "live_state" not in st.session_state:
    st.session_state.live_state = load_state()

STATE = st.session_state.live_state

# Ensure old/missing state fields do not break the app.
STATE.setdefault("transactions", [])
STATE.setdefault("card_overrides", {})
STATE.setdefault("goals", [])
STATE.setdefault("historical_pay", [])
STATE.setdefault("plaid", {})
STATE["plaid"].setdefault("items", [])
STATE["plaid"].setdefault("accounts", [])
STATE["plaid"].setdefault("transactions", [])
STATE["plaid"].setdefault("recurring", {"inflows": [], "outflows": []})
STATE["plaid"].setdefault("last_sync", None)
STATE["plaid"].setdefault("pending_link_token", None)
STATE["plaid"].setdefault("pending_hosted_url", None)
STATE["plaid"].setdefault("pending_update_item_id", None)
STATE["plaid"].setdefault("recurring_overrides", {})
STATE["plaid"].setdefault("income_source_overrides", {})
STATE["plaid"].setdefault("transaction_overrides", {})
STATE["plaid"].setdefault("reconciliation", {"last_run": None, "matched_quick_entries": []})
STATE["settings"].setdefault("visible_future_checks", 8)
STATE["settings"].setdefault("auto_learn_from_plaid_payroll", True)
STATE["settings"].setdefault("recurring_income_sources", {})

STATE["settings"].setdefault("forecast_engine_checks", 26)
STATE["settings"].setdefault("auto_bill_flex_enabled", True)
STATE["settings"].setdefault("bill_flex_rules", {
    "Rent": {
        "enabled": True, "amount": 0.0, "due_day": 1,
        "policy": "next_paycheck", "max_late_days": 14, "fee": 0.0,
    },
    "Truck": {
        "enabled": True, "amount": 0.0, "due_day": 15,
        "policy": "under_30_days_late", "max_late_days": 29, "fee": 0.0,
    },
})
STATE["plaid"].setdefault("account_map", {"checking": None, "savings": None, "cards": {}})
STATE["plaid"].setdefault("use_live_balances", False)
STATE["plaid"].setdefault("last_sync_error", None)
STATE.setdefault("paychecks", {})
STATE.setdefault("settings", {})
STATE["settings"].setdefault("checking_adjustment", 0.0)
STATE["settings"].setdefault("savings_adjustment", 0.0)
STATE["settings"].setdefault("default_max_delay_days", 14)
STATE["settings"].setdefault("smart_income_enabled", True)
STATE["settings"].setdefault("emergency_savings_floor", 1000.0)
STATE["settings"].setdefault("preferred_savings_floor", 3000.0)
STATE["settings"].setdefault("baseline_checking", 0.0)
STATE["settings"].setdefault("baseline_savings", 0.0)
STATE["settings"].setdefault("protected_checking_target", 1000.0)
STATE["settings"].setdefault("savings_rate", 0.25)
STATE["settings"].setdefault("spend_multiplier", 1.0)
STATE["settings"].setdefault("extra_savings_amount", 0.0)
STATE["settings"].setdefault("extra_savings_date", None)
STATE["settings"].setdefault("recurring_bills", [])
STATE["settings"].setdefault("payday_anchor", None)

# ---------- Finance OS 4.1 first-run + isolated demo ----------

def _fresh_state():
    """Return a deep, session-safe Finance OS state with no external connections."""
    return json.loads(json.dumps(default_state()))


def build_demo_state():
    """Realistic synthetic finances. Never uses Plaid, GitHub, or production state."""
    today = datetime.today().date()
    s = _fresh_state()
    s["_storage"] = "session-demo"
    s["_demo"] = True
    settings = s.setdefault("settings", {})
    settings.update({
        "baseline_checking": 6200.0,
        "baseline_savings": 7600.0,
        "protected_checking_target": 1800.0,
        "discretionary_reserve_until_payday": 350.0,
        "emergency_savings_floor": 5000.0,
        "preferred_savings_floor": 10000.0,
        "savings_rate": 0.20,
        "spend_multiplier": 1.0,
        "smart_income_enabled": True,
        "payday_anchor": today.isoformat(),
        "recurring_bills": [
            {"id":"demo-rent","name":"Rent","amount":1950.0,"due_day":1,"payment_method":"Checking","active":True},
            {"id":"demo-car","name":"Car payment","amount":620.0,"due_day":15,"payment_method":"Checking","active":True},
            {"id":"demo-utilities","name":"Utilities","amount":240.0,"due_day":7,"payment_method":"Checking","active":True},
            {"id":"demo-insurance","name":"Insurance","amount":190.0,"due_day":20,"payment_method":"Checking","active":True},
            {"id":"demo-phone","name":"Phone + internet","amount":145.0,"due_day":12,"payment_method":"Checking","active":True},
            {"id":"demo-subs","name":"Subscriptions","amount":72.0,"due_day":25,"payment_method":"Venture","active":True},
        ],
        "cards": [
            {"card":"Venture","balance":2350.0,"limit":10000.0,"due":5,"scheduled":120.0,"action":"Pay down","best_use":"Everyday rewards"},
            {"card":"Savor","balance":640.0,"limit":6000.0,"due":9,"scheduled":85.0,"action":"Maintain","best_use":"Dining"},
            {"card":"Amex Blue","balance":310.0,"limit":8000.0,"due":22,"scheduled":60.0,"action":"Maintain","best_use":"Groceries"},
        ],
        "card_payment_rules": {
            "Venture":{"mode":"fixed","amount":120.0,"due_day":5,"autopay":True,"confirmed":True},
            "Savor":{"mode":"fixed","amount":85.0,"due_day":9,"autopay":True,"confirmed":True},
            "Amex Blue":{"mode":"fixed","amount":60.0,"due_day":22,"autopay":True,"confirmed":True},
        },
    })
    s["card_overrides"] = {
        "Venture":{"balance":2350.0,"limit":10000.0},
        "Savor":{"balance":640.0,"limit":6000.0},
        "Amex Blue":{"balance":310.0,"limit":8000.0},
    }
    nets = [3180, 3420, 3060, 3710, 3290, 3980, 3375, 3560]
    hist=[]
    for i,net in enumerate(nets[::-1]):
        d=today-timedelta(days=14*i)
        hist.append({"date":d.isoformat(),"net":float(net),"description":"Demo payroll"})
    s["historical_pay"] = hist
    s["paychecks"] = {today.isoformat():{"expected":3560.0,"actual":3560.0,"manual":False}}
    s["goals"] = [
        {"id":"demo-emergency","name":"Emergency fund","kind":"emergency","target":12000.0,"deadline":(today+timedelta(days=150)).isoformat(),"priority":"Critical","start_date":(today-timedelta(days=75)).isoformat(),"start_amount":5000.0,"current_override":None,"card":None},
        {"id":"demo-venture","name":"Pay off Venture","kind":"debt","target":0.0,"deadline":(today+timedelta(days=210)).isoformat(),"priority":"High","start_date":(today-timedelta(days=70)).isoformat(),"start_amount":4200.0,"current_override":2350.0,"card":"Venture"},
        {"id":"demo-trip","name":"Dream trip","kind":"purchase","target":4000.0,"deadline":(today+timedelta(days=260)).isoformat(),"priority":"Lifestyle","start_date":(today-timedelta(days=40)).isoformat(),"start_amount":0.0,"current_override":900.0,"saved":900.0,"card":None},
    ]
    tx_specs=[
        (1,"King Soopers",86.42,"FOOD_AND_DRINK","FOOD_AND_DRINK_GROCERIES"),
        (2,"Shell",54.18,"TRANSPORTATION","TRANSPORTATION_GAS"),
        (3,"Chipotle",18.76,"FOOD_AND_DRINK","FOOD_AND_DRINK_RESTAURANT"),
        (4,"Amazon",72.15,"GENERAL_MERCHANDISE","GENERAL_MERCHANDISE_ONLINE_MARKETPLACE"),
        (5,"Target",116.30,"GENERAL_MERCHANDISE","GENERAL_MERCHANDISE_SUPERSTORES"),
        (7,"Spotify",11.99,"ENTERTAINMENT","ENTERTAINMENT_MUSIC_AND_AUDIO"),
        (8,"Coffee Shop",7.85,"FOOD_AND_DRINK","FOOD_AND_DRINK_COFFEE"),
        (10,"Gym",39.00,"PERSONAL_CARE","PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS"),
    ]
    s["plaid"]["transactions"]=[{
        "transaction_id":f"demo-{i}","account_id":"demo-checking","date":(today-timedelta(days=days)).isoformat(),
        "authorized_date":None,"name":name,"merchant_name":name,"amount":amt,"pending":False,
        "category_primary":primary,"category_detailed":detailed,"category_confidence":"VERY_HIGH",
        "payment_channel":"in store","website":"","counterparties":[]
    } for i,(days,name,amt,primary,detailed) in enumerate(tx_specs)]
    s["plaid"].update({"items":[],"accounts":[],"last_sync":None,"use_live_balances":False,"account_map":{"checking":None,"savings":None,"cards":{}}})
    return s


def build_preview_state(data):
    """Build a session-only first route from onboarding answers."""
    today=datetime.today().date()
    s=_fresh_state(); s["_storage"]="session-preview"; s["_preview"]=True
    checking=float(data.get("checking",0) or 0); savings=float(data.get("savings",0) or 0)
    protected=float(data.get("protected",1000) or 0); pay=float(data.get("paycheck",0) or 0)
    next_pay=data.get("next_payday") or (today+timedelta(days=14))
    if isinstance(next_pay,str):
        try: next_pay=date.fromisoformat(next_pay[:10])
        except Exception: next_pay=today+timedelta(days=14)
    anchor=next_pay-timedelta(days=14)
    bills=[]
    for idx,(name,amount,day) in enumerate([
        ("Housing",data.get("housing",0),1),("Car / transportation",data.get("car",0),15),
        ("Utilities",data.get("utilities",0),7),("Insurance",data.get("insurance",0),20),
        ("Phone + internet",data.get("phone",0),12),("Other recurring",data.get("other_bills",0),25),
    ]):
        if float(amount or 0)>0:
            bills.append({"id":f"onboard-{idx}","name":name,"amount":float(amount),"due_day":day,"payment_method":"Checking","active":True})
    card_debt=float(data.get("card_debt",0) or 0); card_limit=max(card_debt,float(data.get("card_limit",0) or 0))
    settings=s["settings"]
    settings.update({
        "baseline_checking":checking,"baseline_savings":savings,"protected_checking_target":protected,
        "discretionary_reserve_until_payday":float(data.get("reserve",0) or 0),"payday_anchor":anchor.isoformat(),
        "recurring_bills":bills,"savings_rate":{"Aggressive":.30,"Balanced":.20,"Comfortable":.10}.get(data.get("pace"),.20),
        "smart_income_enabled":True,
    })
    if card_debt>0:
        settings["cards"]=[{"card":"Primary card","balance":card_debt,"limit":card_limit,"due":18,"scheduled":max(35,min(150,card_debt*.03)),"action":"Pay down","best_use":""}]
        s["card_overrides"]={"Primary card":{"balance":card_debt,"limit":card_limit}}
        settings["card_payment_rules"]={"Primary card":{"mode":"fixed","amount":max(35,min(150,card_debt*.03)),"due_day":18,"autopay":True,"confirmed":True}}
    if pay>0:
        hist=[]
        for i,mult in enumerate([1.00,.96,1.05,.99,1.08,.94,1.03,1.00]):
            d=anchor-timedelta(days=14*i)
            hist.append({"date":d.isoformat(),"net":round(pay*mult,2),"description":"Onboarding income model"})
        s["historical_pay"]=hist
        s["paychecks"]={anchor.isoformat():{"expected":pay,"actual":pay,"manual":True}}
    kind=data.get("goal_kind","savings")
    goal_name=data.get("goal_name") or {"emergency":"Emergency fund","debt":"Pay off debt","purchase":"Big purchase","savings":"Savings goal"}.get(kind,"My goal")
    target=float(data.get("goal_target",0) or 0)
    deadline=data.get("goal_deadline") or today+timedelta(days=180)
    if isinstance(deadline,str): deadline=date.fromisoformat(deadline[:10])
    if target>0:
        start_amount=card_debt if kind=="debt" else (savings if kind in {"savings","emergency"} else 0.0)
        s["goals"]=[{"id":"first-goal","name":goal_name,"kind":kind,"target":target,"deadline":deadline.isoformat(),"priority":data.get("priority","High"),"start_date":today.isoformat(),"start_amount":start_amount,"current_override":card_debt if kind=="debt" else None,"saved":0.0,"card":"Primary card" if kind=="debt" and card_debt>0 else None}]
    return s


def _welcome_css():
    st.markdown("""<style>
    .block-container{max-width:780px;padding-top:3rem}.fos-welcome{padding:34px 30px;border-radius:30px;border:1px solid rgba(255,255,255,.10);background:radial-gradient(circle at 90% 5%,rgba(91,116,255,.25),transparent 32%),linear-gradient(145deg,#101923,#101e34);margin:1rem 0 1.3rem}.fos-mark{font-size:.75rem;letter-spacing:.16em;text-transform:uppercase;opacity:.6}.fos-head{font-size:clamp(2.5rem,7vw,4.6rem);font-weight:860;line-height:1;letter-spacing:-.05em;margin:.7rem 0}.fos-copy{font-size:1.08rem;opacity:.72;max-width:620px;line-height:1.55}.fos-mini{padding:15px 16px;border:1px solid rgba(255,255,255,.08);border-radius:18px;background:rgba(255,255,255,.03);margin:8px 0}.stButton>button{border-radius:15px;min-height:3rem;font-weight:700}</style>""",unsafe_allow_html=True)


def render_welcome():
    _welcome_css()
    st.markdown("<div class='fos-welcome'><div class='fos-mark'>FINANCE OS</div><div class='fos-head'>Your money needs a route.</div><div class='fos-copy'>Finance OS turns your accounts, bills, income and goals into one living plan — what is safe today, what happens next, and how to get where you want to go.</div></div>",unsafe_allow_html=True)
    a,b=st.columns(2)
    with a:
        st.markdown("<div class='fos-mini'><b>Explore instantly</b><br><span style='opacity:.65'>Use realistic fake finances. No bank connection. Nothing saved.</span></div>",unsafe_allow_html=True)
        if st.button("Try Finance OS Demo",use_container_width=True,type="primary"):
            st.session_state.fos_v4_mode="demo"; st.session_state.live_state=build_demo_state(); st.rerun()
    with b:
        st.markdown("<div class='fos-mini'><b>Build your route</b><br><span style='opacity:.65'>Answer a few questions and see your first personalized plan.</span></div>",unsafe_allow_html=True)
        if st.button("Create My Finance OS",use_container_width=True):
            st.session_state.fos_v4_mode="onboarding"; st.session_state.fos_onboarding_step=0; st.session_state.fos_onboarding={}; st.rerun()
    st.caption("Demo data is completely synthetic. Real bank connection stays disabled in this public test build.")


def render_onboarding():
    _welcome_css(); data=st.session_state.setdefault("fos_onboarding",{}); step=int(st.session_state.get("fos_onboarding_step",0))
    titles=["Choose your destination","Where are you today?","How does money come in?","What has to go out?","Choose your comfort level","Your first route"]
    st.caption(f"SETUP · {min(step+1,6)} OF 6")
    st.progress(min(1.0,(step+1)/6))
    st.markdown(f"## {titles[min(step,5)]}")
    if step==0:
        with st.form("onboard_goal"):
            kind=st.selectbox("What matters most right now?",["emergency","debt","savings","purchase"],format_func=lambda x:{"emergency":"Build an emergency fund","debt":"Pay off debt","savings":"Grow savings","purchase":"Save for a purchase"}[x])
            name=st.text_input("Give the goal a name",value=data.get("goal_name","") or "")
            target=st.number_input("Target amount",min_value=100.0,value=float(data.get("goal_target",5000) or 5000),step=100.0)
            deadline=st.date_input("I want to reach it by",value=datetime.today().date()+timedelta(days=180),min_value=datetime.today().date())
            priority=st.selectbox("Priority",["Critical","High","Normal","Lifestyle"],index=1)
            if st.form_submit_button("Continue",use_container_width=True,type="primary"):
                data.update(goal_kind=kind,goal_name=name.strip() or None,goal_target=target,goal_deadline=deadline.isoformat(),priority=priority); st.session_state.fos_onboarding_step=1; st.rerun()
    elif step==1:
        with st.form("onboard_now"):
            checking=st.number_input("Checking balance",min_value=0.0,value=float(data.get("checking",3500) or 0),step=100.0)
            savings=st.number_input("Savings balance",min_value=0.0,value=float(data.get("savings",2000) or 0),step=100.0)
            debt=st.number_input("Credit-card debt",min_value=0.0,value=float(data.get("card_debt",0) or 0),step=100.0)
            limit=st.number_input("Total credit limit",min_value=0.0,value=float(data.get("card_limit",5000) or 0),step=500.0)
            if st.form_submit_button("Continue",use_container_width=True,type="primary"):
                data.update(checking=checking,savings=savings,card_debt=debt,card_limit=limit); st.session_state.fos_onboarding_step=2; st.rerun()
    elif step==2:
        with st.form("onboard_income"):
            pay=st.number_input("Typical take-home paycheck",min_value=0.0,value=float(data.get("paycheck",2500) or 0),step=100.0)
            next_pay=st.date_input("Next payday",value=datetime.today().date()+timedelta(days=14),min_value=datetime.today().date()+timedelta(days=1))
            st.caption("This beta models biweekly pay first. Variable-income intelligence will learn from connected history later.")
            if st.form_submit_button("Continue",use_container_width=True,type="primary"):
                data.update(paycheck=pay,next_payday=next_pay.isoformat()); st.session_state.fos_onboarding_step=3; st.rerun()
    elif step==3:
        with st.form("onboard_bills"):
            c1,c2=st.columns(2); housing=c1.number_input("Housing / rent",min_value=0.0,value=float(data.get("housing",1500) or 0),step=50.0); car=c2.number_input("Car / transportation",min_value=0.0,value=float(data.get("car",400) or 0),step=50.0)
            c3,c4=st.columns(2); utilities=c3.number_input("Utilities",min_value=0.0,value=float(data.get("utilities",200) or 0),step=25.0); insurance=c4.number_input("Insurance",min_value=0.0,value=float(data.get("insurance",150) or 0),step=25.0)
            c5,c6=st.columns(2); phone=c5.number_input("Phone + internet",min_value=0.0,value=float(data.get("phone",120) or 0),step=10.0); other=c6.number_input("Other recurring",min_value=0.0,value=float(data.get("other_bills",100) or 0),step=25.0)
            if st.form_submit_button("Continue",use_container_width=True,type="primary"):
                data.update(housing=housing,car=car,utilities=utilities,insurance=insurance,phone=phone,other_bills=other); st.session_state.fos_onboarding_step=4; st.rerun()
    elif step==4:
        with st.form("onboard_comfort"):
            protected=st.number_input("Checking cushion I never want to cross",min_value=0.0,value=float(data.get("protected",1000) or 0),step=100.0)
            reserve=st.number_input("Guilt-free spending reserve until payday",min_value=0.0,value=float(data.get("reserve",250) or 0),step=50.0)
            pace=st.radio("How should Finance OS pace goals?",["Aggressive","Balanced","Comfortable"],index=1,horizontal=True)
            if st.form_submit_button("Build my route",use_container_width=True,type="primary"):
                data.update(protected=protected,reserve=reserve,pace=pace); st.session_state.fos_onboarding_step=5; st.rerun()
    else:
        preview=build_preview_state(data)
        st.markdown("<div class='fos-welcome'><div class='fos-mark'>ROUTE READY</div><div class='fos-head'>You have a starting plan.</div><div class='fos-copy'>Finance OS has enough to build a first cash runway and goal pace. In the full product, connecting your banks replaces these estimates with live balances and transaction history.</div></div>",unsafe_allow_html=True)
        st.write(f"**Primary goal:** {data.get('goal_name') or data.get('goal_kind','Goal').title()} · ${float(data.get('goal_target',0)):,.0f}")
        st.write(f"**Safety cushion:** ${float(data.get('protected',0)):,.0f} · **Typical paycheck:** ${float(data.get('paycheck',0)):,.0f}")
        if st.button("Open My Finance OS",use_container_width=True,type="primary"):
            st.session_state.live_state=preview; st.session_state.fos_v4_mode="personal_preview"; st.rerun()
    if step>0 and step<5:
        if st.button("← Back"):
            st.session_state.fos_onboarding_step=max(0,step-1); st.rerun()
    if st.button("Exit setup"):
        st.session_state.fos_v4_mode="welcome"; st.rerun()


if "fos_v4_mode" not in st.session_state:
    st.session_state.fos_v4_mode = "welcome"

if st.session_state.fos_v4_mode == "welcome":
    render_welcome(); st.stop()
if st.session_state.fos_v4_mode == "onboarding":
    render_onboarding(); st.stop()

if st.session_state.fos_v4_mode == "demo" and not STATE.get("_demo"):
    STATE = build_demo_state(); st.session_state.live_state = STATE
elif st.session_state.fos_v4_mode == "personal_preview" and not STATE.get("_preview"):
    STATE = build_preview_state(st.session_state.get("fos_onboarding", {})); st.session_state.live_state = STATE

# Keep legacy BASE consumers working, but source the values from Finance OS state
# rather than an external workbook. This is a compatibility bridge while the
# remaining engine is progressively converted to first-class state helpers.
BASE.update({
    "as_of": datetime.today(),
    "current_checking": float(STATE["settings"].get("baseline_checking", 0) or 0),
    "current_savings": float(STATE["settings"].get("baseline_savings", 0) or 0),
    "starting_checking": float(STATE["settings"].get("baseline_checking", 0) or 0),
    "starting_savings": float(STATE["settings"].get("baseline_savings", 0) or 0),
    "protected_buffer": float(STATE["settings"].get("protected_checking_target", 1000) or 1000),
    "savings_rate": float(STATE["settings"].get("savings_rate", 0.25) or 0),
    "extra_savings_amount": float(STATE["settings"].get("extra_savings_amount", 0) or 0),
    "extra_savings_date": STATE["settings"].get("extra_savings_date"),
    "spend_multiplier": float(STATE["settings"].get("spend_multiplier", 1.0) or 1.0),
    "recurring_bills": list(STATE["settings"].get("recurring_bills", []) or []),
    "cards": list(STATE["settings"].get("cards", []) or []),
})

for row in BASE["forecast"]:
    key = row["date"].date().isoformat()
    STATE["paychecks"].setdefault(key, {"expected": row["income"], "actual": None, "manual": False})
for c in BASE["cards"]:
    if c["card"]:
        STATE["card_overrides"].setdefault(
            c["card"],
            {"balance": c["balance"], "limit": c["limit"]},
        )

def plaid_account_by_id(account_id):
    if not account_id:
        return None
    for account in STATE.get("plaid", {}).get("accounts", []):
        if account.get("account_id") == account_id:
            return account
    return None


def plaid_last_sync_dt():
    raw = STATE.get("plaid", {}).get("last_sync")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except Exception:
        return None


def plaid_live_balances_enabled():
    """
    True when Finance OS has synced Plaid data and at least one mapped cash/card account.
    v3 no longer uses the obsolete manual `use_live_balances` switch as a master kill switch.
    """
    plaid = STATE.get("plaid", {}) or {}
    mapping = plaid.get("account_map", {}) or {}
    mapped_cards = mapping.get("cards", {}) or {}
    has_mapping = bool(mapping.get("checking") or mapping.get("savings") or any(mapped_cards.values()))
    return bool(plaid.get("last_sync") and plaid.get("accounts") and has_mapping)


def quick_entry_matches_plaid(qtx, ptx):
    """
    Conservative match used only to avoid double-counting.
    Requires similar amount, nearby date, and compatible direction/account context.
    """
    try:
        q_amt = float(qtx.get("amount", 0) or 0)
        p_amt = float(ptx.get("amount", 0) or 0)
    except Exception:
        return False

    q_type = qtx.get("type")
    # Plaid: outflows positive, inflows negative.
    if q_type == "Income":
        target = -q_amt
    else:
        target = q_amt

    if abs(target - p_amt) > max(0.75, abs(target) * .01):
        return False

    try:
        q_date = datetime.fromisoformat(str(qtx.get("date"))).date()
        p_date = datetime.fromisoformat(str(ptx.get("date"))).date()
    except Exception:
        return False

    if abs((q_date - p_date).days) > 3:
        return False

    q_card = qtx.get("card")
    if q_card:
        mapped_id = (STATE.get("plaid", {}).get("account_map", {}).get("cards") or {}).get(q_card)
        if mapped_id and ptx.get("account_id") != mapped_id:
            return False

    return True


def reconcile_quick_entries_with_plaid():
    plaid_txs = [
        x for x in STATE.get("plaid", {}).get("transactions", [])
        if not x.get("pending")
    ]
    matched_ids = set()
    used_plaid = set()

    for qtx in STATE.get("transactions", []):
        qid = qtx.get("id")
        if not qid:
            continue
        for ptx in plaid_txs:
            pid = ptx.get("transaction_id")
            if not pid or pid in used_plaid:
                continue
            if quick_entry_matches_plaid(qtx, ptx):
                matched_ids.add(qid)
                used_plaid.add(pid)
                break

    STATE.setdefault("plaid", {}).setdefault(
        "reconciliation",
        {"last_run": None, "matched_quick_entries": []},
    )
    STATE["plaid"]["reconciliation"] = {
        "last_run": datetime.now().isoformat(timespec="seconds"),
        "matched_quick_entries": sorted(matched_ids),
    }
    return matched_ids


def transaction_source_label():
    if plaid_live_balances_enabled():
        return "Plaid live + unsynced Quick Entry"
    if STATE.get("plaid", {}).get("last_sync"):
        return "Finance OS balances • Plaid analytics"
    return "Finance OS live state"


def unsynced_live_entries():
    """
    When Plaid balances are authoritative, layer only entries that Plaid has not
    already caught up with. This prevents a manual Quick Entry and its later bank
    transaction from being counted twice.
    """
    last_sync = plaid_last_sync_dt()
    if not last_sync:
        return []

    matched = set(
        STATE.get("plaid", {})
        .get("reconciliation", {})
        .get("matched_quick_entries", [])
    )

    rows = []
    for tx in STATE.get("transactions", []):
        if tx.get("id") in matched:
            continue

        raw = tx.get("created_at")
        if raw:
            try:
                created = datetime.fromisoformat(str(raw))
                if created > last_sync:
                    rows.append(tx)
                    continue
            except Exception:
                pass

        # Legacy/manual entries without reliable timestamps can still remain live if
        # they are not matched to any Plaid transaction and are dated after the last sync date.
        try:
            tx_day = datetime.fromisoformat(str(tx.get("date"))).date()
            if tx_day >= last_sync.date():
                rows.append(tx)
        except Exception:
            continue

    return rows

def plaid_mapped_balance(kind, card_name=None):
    """
    Return the Plaid balance Finance OS should use for planning.

    Checking uses AVAILABLE balance first because that reflects cash that can
    actually be spent right now after pending holds/authorizations. Current
    balance is only a fallback when Plaid does not provide available.

    Savings and credit cards continue to use current balance as the primary
    source because "available" means something different for those account
    types (especially credit, where available is remaining credit rather than
    debt balance).
    """
    if not plaid_live_balances_enabled():
        return None

    mapping = STATE.get("plaid", {}).get("account_map", {})
    if kind == "card":
        account_id = (mapping.get("cards") or {}).get(card_name)
    else:
        account_id = mapping.get(kind)

    account = plaid_account_by_id(account_id)
    if not account:
        return None

    if kind == "checking":
        # Plaid institutions do not all populate "available" consistently. A checking
        # available balance should not materially exceed current balance. If it does,
        # treat it as institution metadata that is unsafe for cash planning and use current.
        try:
            current = float(account.get("current")) if account.get("current") is not None else None
        except (TypeError, ValueError):
            current = None
        try:
            available = float(account.get("available")) if account.get("available") is not None else None
        except (TypeError, ValueError):
            available = None

        if available is not None and current is not None:
            # Normal pending debit holds make available <= current. Allow a tiny
            # tolerance for institution rounding/credits, but never thousands above current.
            if available <= current + 5.0:
                return available
            return current
        if available is not None:
            return available
        return current

    candidates = [account.get("current"), account.get("available")]
    for value in candidates:
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None



def openai_settings():
    """
    Read optional OpenAI API configuration from Streamlit Secrets.

    Recommended:
      [openai]
      api_key = "sk-..."
      model = "gpt-5.6-terra"

    The API key never enters saved Finance OS state.
    """
    try:
        cfg = st.secrets.get("openai", {})
        api_key = cfg.get("api_key")
        if not api_key:
            return None
        return {
            "api_key": str(api_key),
            "model": str(cfg.get("model", "gpt-5.6-terra")),
            "reasoning_effort": str(cfg.get("reasoning_effort", "medium")),
        }
    except Exception:
        return None


def _extract_openai_response_text(payload):
    """
    Robust Responses API text extraction.

    Supports direct output_text plus nested output/content shapes. The API can return
    reasoning items alongside message items, so we only collect user-visible text-like
    fields and safely tolerate future wrapper objects.
    """
    if not isinstance(payload, dict):
        return ""

    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parts = []

    def collect(node, parent_key=None):
        if isinstance(node, str):
            if parent_key in {"text", "output_text", "value"} and node.strip():
                parts.append(node.strip())
            return

        if isinstance(node, list):
            for x in node:
                collect(x, parent_key=parent_key)
            return

        if not isinstance(node, dict):
            return

        node_type = str(node.get("type") or "").lower()

        # Canonical Responses API visible-text content item.
        if node_type in {"output_text", "text"}:
            value = node.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
            elif isinstance(value, dict):
                nested = value.get("value")
                if isinstance(nested, str) and nested.strip():
                    parts.append(nested.strip())

        # Some wrappers expose a direct text/value without a type.
        for key in ("output_text", "text"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
            elif isinstance(value, dict):
                nested = value.get("value")
                if isinstance(nested, str) and nested.strip():
                    parts.append(nested.strip())

        # Recurse through likely response containers.
        for key in ("output", "content", "message", "messages", "response", "result"):
            if key in node:
                collect(node.get(key), parent_key=key)

    collect(payload)

    # Preserve order while de-duplicating repeated extraction paths.
    seen = set()
    cleaned = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            cleaned.append(p)
    return "\n\n".join(cleaned).strip()


def _openai_no_text_reason(payload):
    """Human-readable diagnostic for a successful API response with no visible text."""
    if not isinstance(payload, dict):
        return "The API returned an unexpected response shape."
    status = str(payload.get("status") or "").strip()
    incomplete = payload.get("incomplete_details") or {}
    reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
    if status == "incomplete" and reason:
        return f"The model response was incomplete ({reason})."
    if status:
        return f"The API response status was {status}, but no visible assistant text was present."
    return "The API returned no visible assistant text."


def call_finance_ai(snapshot, user_question=None):
    """
    Ask OpenAI to audit/explain Finance OS's deterministic recommendation.

    Important architecture rule:
    AI receives calculated facts and recommendations, but it does not write state,
    change balances, create payments, or replace Finance OS's calculation engine.
    """
    cfg = openai_settings()
    if not cfg:
        raise RuntimeError("OpenAI API is not configured in Streamlit Secrets.")

    system_prompt = """
You are the second-opinion financial decision layer inside a private personal-finance app.

Your job is to give clear, conservative, practical household-finance guidance from the
structured snapshot supplied by Finance OS. Treat the supplied balances, forecasts,
card limits, payment dates, and deterministic recommendations as the source of truth.

Decision priority:
1. Keep required obligations fundable and avoid late payments.
2. Preserve a sensible near-term cash/emergency cushion.
3. Avoid paying credit-card interest unnecessarily when genuine surplus cash exists.
4. Reduce dangerous credit utilization when useful.
5. Rebuild savings and optimize only after the first four priorities are protected.

Do NOT recommend carrying a credit-card balance merely to improve utilization or keep
extra idle cash above an already-protected liquidity floor. If a card can be paid in
full from genuine surplus without endangering obligations or the protected liquidity
floor, say so plainly.

Do NOT invent transactions, balances, APRs, due dates, fees, income, or facts that are
not present. If APR is unknown, do not rank cards by interest rate. If the snapshot is
insufficient for a confident recommendation, identify exactly what is missing.

Keep the answer easy to follow. Start with one short 'Best move' statement, then explain
why, what to do next, and any important caution. Explicitly call out any place where
the deterministic Finance OS recommendation appears overly conservative or aggressive.
Do not present yourself as a fiduciary, CPA, attorney, or licensed financial adviser.
""".strip()

    question = (
        user_question.strip()
        if isinstance(user_question, str) and user_question.strip()
        else "Review my current financial position and tell me the smartest next move."
    )

    body = {
        "model": cfg["model"],
        "reasoning": {"effort": cfg["reasoning_effort"]},
        "max_output_tokens": 900,
        "input": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "Question:\n" + question +
                    "\n\nFinance OS structured snapshot:\n" +
                    json.dumps(snapshot, indent=2, default=str)
                ),
            },
        ],
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
            "User-Agent": "Finance-OS-Streamlit/0.9.0.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=75) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = (
                (detail.get("error") or {}).get("message")
                or detail.get("message")
                or str(detail)
            )
        except Exception:
            message = str(exc)
        raise RuntimeError(f"OpenAI API error: {message}") from exc
    except Exception as exc:
        raise RuntimeError(f"AI review could not be completed: {exc}") from exc

    output = _extract_openai_response_text(payload)
    if not output:
        raise RuntimeError("OpenAI returned no readable review text.")

    usage = payload.get("usage", {}) if isinstance(payload, dict) else {}
    return {
        "text": output,
        "model": cfg["model"],
        "usage": usage,
        "response_id": payload.get("id"),
    }


# ---------- Live calculations ----------

def money(value):
    try:
        value = float(value)
        sign = "-" if value < 0 else ""
        return f"{sign}${abs(value):,.0f}"
    except Exception:
        return str(value)


def md_money(value):
    """Currency safe for Streamlit Markdown/caption/info/write text."""
    return money(value).replace("$", "&#36;")


def md_text(value):
    """Make arbitrary pre-built text safe from Streamlit dollar-sign math parsing."""
    return str(value).replace("$", "&#36;")


def money2(value):
    try:
        value = float(value)
        sign = "-" if value < 0 else ""
        return f"{sign}${abs(value):,.2f}"
    except Exception:
        return str(value)

def fmt_date(value):
    if isinstance(value, datetime):
        return value.strftime("%b %d").replace(" 0", " ")
    if isinstance(value, date):
        return value.strftime("%b %d").replace(" 0", " ")
    if isinstance(value, (int, float)) and 1 <= value <= 80000:
        d = datetime(1899, 12, 30) + timedelta(days=float(value))
        return d.strftime("%b %d").replace(" 0", " ")
    if isinstance(value, str):
        try:
            d = datetime.fromisoformat(value)
            return d.strftime("%b %d").replace(" 0", " ")
        except Exception:
            return value
    return "—" if value is None else str(value)

def risk_class(label):
    label = str(label).upper()
    if label in {"CLEAR", "GOOD", "COMFORTABLE", "LOW"}:
        return "good"
    if label in {"WATCH", "TIGHT", "RECOVERING", "MODERATE"}:
        return "warn"
    return "bad"


def tx_date(tx):
    try:
        return datetime.fromisoformat(str(tx.get("date")))
    except Exception:
        return BASE["as_of"]

def is_purchase(tx):
    return tx.get("type") in {"Card purchase", "Expense"}

def live_card_rows():
    rows = []
    base_by_name = {str(x.get("card")): dict(x) for x in BASE.get("cards", []) if x.get("card")}

    names = set(base_by_name)
    names.update(str(x) for x in STATE.get("card_overrides", {}).keys() if x)
    mapped = STATE.get("plaid", {}).get("account_map", {}).get("cards", {}) or {}
    names.update(str(x) for x in mapped.keys() if x)

    for name in sorted(names):
        base = base_by_name.get(name, {
            "card": name, "balance": 0.0, "limit": 0.0, "due": None,
            "scheduled": 0.0, "action": "", "best_use": "",
        })
        override = STATE["card_overrides"].get(name, {})
        raw_limit = float(override.get("limit", base.get("limit", 0)) or 0)
        limit_known = raw_limit > 1.0
        limit = raw_limit if limit_known else 0.0

        plaid_balance = plaid_mapped_balance("card", card_name=name)
        if plaid_balance is not None:
            balance = max(0.0, plaid_balance)
            tx_source = unsynced_live_entries()
        else:
            balance = float(override.get("balance", base.get("balance", 0)) or 0)
            tx_source = STATE["transactions"]

        for tx in tx_source:
            if tx.get("card") != name:
                continue
            amount = float(tx.get("amount", 0) or 0)
            if tx.get("type") == "Card purchase":
                balance += amount
            elif tx.get("type") == "Card payment":
                balance = max(0, balance - amount)

        available = max(0, limit - balance) if limit_known else 0.0
        util = balance / limit if limit_known and limit > 0 else None
        rows.append({
            **base,
            "balance": balance,
            "limit": limit,
            "limit_known": limit_known,
            "available": available,
            "util": util if util is not None else 0.0,
            "balance_source": "Plaid" if plaid_balance is not None else "Finance OS",
        })
    return rows


def live_current_savings():
    plaid_balance = plaid_mapped_balance("savings")
    if plaid_balance is not None:
        savings = max(0.0, plaid_balance)
        tx_source = unsynced_live_entries()
    else:
        savings = BASE["current_savings"] + float(
            STATE["settings"].get("savings_adjustment", 0) or 0
        )
        tx_source = STATE["transactions"]

    for tx in tx_source:
        d = tx_date(tx)
        if d.date() > BASE["as_of"].date():
            continue
        amount = float(tx.get("amount", 0) or 0)
        if tx.get("type") == "Transfer to savings":
            savings += amount
        elif tx.get("type") == "Transfer from savings":
            savings = max(0.0, savings - amount)
    return savings

def sales_period_label(d):
    """Retail-sales windows used only to explain the user's own historical pay."""
    if isinstance(d, datetime):
        d = d.date()
    md = (d.month, d.day)
    if (2, 10) <= md <= (2, 25):
        return "Presidents Day"
    if (5, 15) <= md <= (5, 31):
        return "Memorial Day"
    if (6, 24) <= md <= (7, 8):
        return "July 4"
    if (8, 20) <= md <= (9, 10):
        return "Labor Day"
    if (11, 15) <= md <= (12, 2):
        return "Black Friday"
    if (12, 15) <= md <= (12, 31):
        return "Christmas selling period"
    return None


ADP_BOOTSTRAP_TAKE_HOME = [
    {"date": "2024-01-05", "net": 3385.06, "source": "ADP screenshot seed"},
    {"date": "2024-01-19", "net": 3283.11, "source": "ADP screenshot seed"},
    {"date": "2024-02-02", "net": 3462.18, "source": "ADP screenshot seed"},
    {"date": "2024-02-16", "net": 3031.72, "source": "ADP screenshot seed"},
    {"date": "2024-03-01", "net": 3546.07, "source": "ADP screenshot seed"},
    {"date": "2024-03-15", "net": 3420.83, "source": "ADP screenshot seed"},
    {"date": "2024-03-29", "net": 3506.14, "source": "ADP screenshot seed"},
    {"date": "2024-04-12", "net": 3476.51, "source": "ADP screenshot seed"},
    {"date": "2024-04-26", "net": 3411.12, "source": "ADP screenshot seed"},
    {"date": "2024-05-10", "net": 3351.18, "source": "ADP screenshot seed"},
    {"date": "2024-05-24", "net": 3066.83, "source": "ADP screenshot seed"},
    {"date": "2024-06-07", "net": 3824.56, "source": "ADP screenshot seed"},
    {"date": "2024-06-21", "net": 3258.38, "source": "ADP screenshot seed"},
    {"date": "2024-07-05", "net": 2447.49, "source": "ADP screenshot seed"},
    {"date": "2024-07-19", "net": 3723.15, "source": "ADP screenshot seed"},
    {"date": "2024-08-02", "net": 5554.96, "source": "ADP screenshot seed"},
    {"date": "2024-08-16", "net": 4334.60, "source": "ADP screenshot seed"},
    {"date": "2024-08-30", "net": 3441.85, "source": "ADP screenshot seed"},
    {"date": "2024-09-13", "net": 6308.82, "source": "ADP screenshot seed"},
    {"date": "2024-09-27", "net": 3731.48, "source": "ADP screenshot seed"},
    {"date": "2024-10-11", "net": 2594.91, "source": "ADP screenshot seed"},
    {"date": "2024-10-25", "net": 3187.71, "source": "ADP screenshot seed"},
    {"date": "2024-11-08", "net": 3655.59, "source": "ADP screenshot seed"},
    {"date": "2024-11-22", "net": 2903.66, "source": "ADP screenshot seed"},
    {"date": "2024-12-06", "net": 4731.66, "source": "ADP screenshot seed"},
    {"date": "2024-12-20", "net": 2204.12, "source": "ADP screenshot seed"},
    {"date": "2025-01-03", "net": 2501.65, "source": "ADP screenshot seed"},
    {"date": "2025-01-17", "net": 3186.60, "source": "ADP screenshot seed"},
    {"date": "2025-01-31", "net": 10714.46, "source": "ADP screenshot seed"},
    {"date": "2025-02-14", "net": 5167.08, "source": "ADP screenshot seed"},
    {"date": "2025-02-28", "net": 3414.61, "source": "ADP screenshot seed"},
    {"date": "2025-03-14", "net": 3588.90, "source": "ADP screenshot seed"},
    {"date": "2025-03-28", "net": 2928.77, "source": "ADP screenshot seed"},
    {"date": "2025-04-11", "net": 3617.06, "source": "ADP screenshot seed"},
    {"date": "2025-04-25", "net": 3644.71, "source": "ADP screenshot seed"},
    {"date": "2025-05-09", "net": 4814.63, "source": "ADP screenshot seed"},
    {"date": "2025-05-23", "net": 3266.08, "source": "ADP screenshot seed"},
    {"date": "2025-06-06", "net": 6419.87, "source": "ADP screenshot seed"},
    {"date": "2025-06-20", "net": 2821.42, "source": "ADP screenshot seed"},
    {"date": "2025-07-03", "net": 3057.60, "source": "ADP screenshot seed"},
    {"date": "2025-07-18", "net": 4760.01, "source": "ADP screenshot seed"},
    {"date": "2025-08-01", "net": 3665.35, "source": "ADP screenshot seed"},
    {"date": "2025-08-15", "net": 4881.93, "source": "ADP screenshot seed"},
    {"date": "2025-08-29", "net": 3121.30, "source": "ADP screenshot seed"},
    {"date": "2025-09-12", "net": 6740.22, "source": "ADP screenshot seed"},
    {"date": "2025-09-26", "net": 3602.68, "source": "ADP screenshot seed"},
    {"date": "2025-10-10", "net": 2942.67, "source": "ADP screenshot seed"},
    {"date": "2025-10-24", "net": 2495.98, "source": "ADP screenshot seed"},
    {"date": "2025-11-07", "net": 4079.91, "source": "ADP screenshot seed"},
    {"date": "2025-11-21", "net": 4397.86, "source": "ADP screenshot seed"},
    {"date": "2025-12-05", "net": 3797.85, "source": "ADP screenshot seed"},
    {"date": "2025-12-19", "net": 5361.08, "source": "ADP screenshot seed"},
]

def historical_pay_rows():
    """
    Unified, self-learning paycheck history.

    Seed/imported history gets Finance OS started. Verified Plaid payroll deposits
    automatically become actual paychecks and are added to future seasonal matching.
    """
    if _RENDER_CACHE["historical_pay"] is not None:
        return _RENDER_CACHE["historical_pay"]

    merged = {}

    # Bootstrap history from the ADP screenshots supplied during setup.
    # This is only the starting history; new Plaid payroll actuals continue learning automatically.
    for item in ADP_BOOTSTRAP_TAKE_HOME:
        try:
            d = datetime.fromisoformat(str(item["date"])).date()
            amount = float(item["net"])
        except Exception:
            continue
        if amount > 0:
            merged[d.isoformat()] = {
                "date": d,
                "net": amount,
                "source": item.get("source", "ADP screenshot seed"),
                "actual": True,
            }

    for item in STATE.get("historical_pay", []):
        try:
            d = datetime.fromisoformat(str(item.get("date"))).date()
            amount = float(item.get("net", 0) or 0)
        except Exception:
            continue
        if amount > 0:
            merged[d.isoformat()] = {
                "date": d,
                "net": amount,
                "source": item.get("source", "Seed history"),
                "actual": True,
            }

    if STATE["settings"].get("auto_learn_from_plaid_payroll", True):
        for item in plaid_detected_paychecks():
            d = item["date"]
            amount = float(item.get("net", 0) or 0)
            if amount <= 0:
                continue
            merged[d.isoformat()] = {
                "date": d,
                "net": amount,
                "source": "Plaid actual payroll",
                "description": item.get("description", "Payroll"),
                "transaction_id": item.get("transaction_id"),
                "actual": True,
            }

    rows = sorted(merged.values(), key=lambda x: x["date"])

    # Monthly ordinal is for display/auditing only, not forecasting semantics.
    groups = {}
    for row in rows:
        groups.setdefault((row["date"].year, row["date"].month), []).append(row)
    for _, month_rows in groups.items():
        month_rows.sort(key=lambda x: x["date"])
        for idx, row in enumerate(month_rows, start=1):
            row["monthly_check_number"] = idx
            row["check_type"] = f"Check {idx}"

    _RENDER_CACHE["historical_pay"] = rows
    return _RENDER_CACHE["historical_pay"]


def actual_paycheck_for_scheduled_date(target_date, tolerance_days=4):
    """Match a posted Plaid payroll deposit to a scheduled payday."""
    if isinstance(target_date, datetime):
        target_date = target_date.date()
    candidates = []
    for item in historical_pay_rows():
        if item.get("source") != "Plaid actual payroll":
            continue
        delta = abs((item["date"] - target_date).days)
        if delta <= int(tolerance_days):
            candidates.append((delta, item))
    return min(candidates, key=lambda x: x[0])[1] if candidates else None


def payroll_learning_summary():
    hist = historical_pay_rows()
    plaid = [x for x in hist if x.get("source") == "Plaid actual payroll"]
    seed = [x for x in hist if x.get("source") != "Plaid actual payroll"]
    return {
        "total": len(hist),
        "plaid_actuals": len(plaid),
        "seed": len(seed),
        "latest_actual": max((x["date"] for x in plaid), default=None),
    }


def paycheck_cycle_strength(target_date):
    """
    Label the expected seasonal strength without assuming a 'bonus check'.
    """
    model = closest_historical_pay_analogs(target_date, years_back=2, window_days=24)
    if not model:
        return "Typical-income cycle"
    avg = sum(x["net"] for x in model) / len(model)
    all_hist = sorted(x["net"] for x in historical_pay_rows())
    if len(all_hist) < 4:
        return "Typical-income cycle"
    q35 = all_hist[max(0, int(round((len(all_hist)-1)*.35)))]
    q65 = all_hist[max(0, int(round((len(all_hist)-1)*.65)))]
    if avg >= q65:
        return "Higher-income cycle"
    if avg <= q35:
        return "Lower-income cycle"
    return "Typical-income cycle"



def biweekly_payday_dates(count=26, from_date=None):
    """
    Rolling biweekly schedule anchored to Finance OS state.

    Priority: saved paycheck overrides -> historical payroll -> explicit payday
    anchor. This removes the last scheduling dependency on the old workbook.
    """
    known = []

    for raw in STATE.get("paychecks", {}).keys():
        try:
            known.append(date.fromisoformat(str(raw)[:10]))
        except Exception:
            pass

    if not known:
        try:
            known.extend(x["date"] for x in historical_pay_rows() if x.get("date"))
        except Exception:
            pass

    anchor_raw = STATE.get("settings", {}).get("payday_anchor")
    if anchor_raw:
        try:
            known.append(date.fromisoformat(str(anchor_raw)[:10]))
        except Exception:
            pass

    known = sorted(set(d for d in known if isinstance(d, date)))
    if not known:
        return []

    today = from_date or datetime.today().date()
    before = [d for d in known if d <= today]
    anchor = before[-1] if before else known[0]

    d = anchor
    while d < today:
        d += timedelta(days=14)
    while d - timedelta(days=14) >= today:
        d -= timedelta(days=14)

    return [d + timedelta(days=14*i) for i in range(int(count))]


def is_first_paycheck_of_month(target_date):
    if isinstance(target_date, datetime):
        target_date = target_date.date()

    # Generate enough surrounding dates to determine check order in the month.
    anchor_start = target_date - timedelta(days=35)
    dates = biweekly_payday_dates(count=8, from_date=anchor_start)
    same_month = sorted(
        d for d in dates
        if d.year == target_date.year and d.month == target_date.month
    )
    if same_month:
        return target_date == same_month[0]

    return target_date.day <= 15



def closest_historical_pay_analogs(target_date, years_back=2, window_days=24):
    """
    For each prior year, find the actual paycheck closest to the same calendar point.
    Example: a Sep 11, 2026 forecast first looks near Sep 11, 2025, then Sep 11, 2024.
    This is intentionally more literal than the older same-month/same-half weighting.
    """
    if isinstance(target_date, datetime):
        target_date = target_date.date()

    hist = historical_pay_rows()
    analogs = []

    for years in range(1, int(years_back) + 1):
        try:
            anchor = target_date.replace(year=target_date.year - years)
        except ValueError:
            # Feb 29 -> Feb 28 in non-leap years.
            anchor = date(target_date.year - years, target_date.month, 28)

        candidates = []
        for item in hist:
            if item["date"].year != anchor.year:
                continue
            delta = abs((item["date"] - anchor).days)
            if delta <= window_days:
                candidates.append((delta, item))

        if candidates:
            delta, item = min(candidates, key=lambda x: (x[0], -x[1]["net"]))
            analogs.append({
                "year": anchor.year,
                "anchor": anchor,
                "date": item["date"],
                "net": item["net"],
                "days_from_match": delta,
                "season": sales_period_label(item["date"]),
                "check_type": item.get("check_type", "Regular"),
            })

    return analogs


def smart_paycheck_estimate(target_date, fallback=0.0):
    """
    Personalized paycheck estimate.

    Priority:
      1) nearest comparable paycheck from last year,
      2) nearest comparable paycheck from two years ago,
      3) recent pay trend as a modest stabilizer,
      4) workbook fallback only when history is insufficient.

    Retail-sales periods still help explain the estimate, but they no longer override
    the literal closest historical paycheck match.
    """
    if isinstance(target_date, datetime):
        target_date = target_date.date()

    hist = historical_pay_rows()
    if len(hist) < 4:
        return {
            "estimate": float(fallback or 0),
            "low": float(fallback or 0),
            "high": float(fallback or 0),
            "confidence": "Low",
            "reason": "Not enough historical pay data yet",
            "samples": len(hist),
            "analogs": [],
            "two_year_average": None,
            "last_year_match": None,
            "two_years_ago_match": None,
            "target_check_type": "Typical-income cycle",
        }

    analogs = closest_historical_pay_analogs(target_date, years_back=2, window_days=24)
    recent = [x["net"] for x in hist[-6:]]
    recent_mean = sum(recent) / len(recent) if recent else float(fallback or 0)

    if analogs:
        weighted = []
        for a in analogs:
            # Last year matters more than two years ago, and closer dates matter more.
            recency_weight = 1.50 if a["year"] == target_date.year - 1 else 1.00
            proximity_weight = max(0.35, 1.0 - a["days_from_match"] / 32.0)
            season_match = (
                1.15
                if sales_period_label(target_date)
                and a["season"] == sales_period_label(target_date)
                else 1.0
            )
            weighted.append((a["net"], recency_weight * proximity_weight * season_match))

        total_w = sum(w for _, w in weighted)
        analog_mean = sum(v * w for v, w in weighted) / total_w

        two_year_average_pre = sum(a["net"] for a in analogs) / len(analogs)

        # Seasonal weighting: last year's comparable check is the primary anchor.
        # Two years ago remains visible and still influences the estimate.
        if len(analogs) >= 2:
            analogs_by_year = sorted(analogs, key=lambda a: a["year"], reverse=True)
            last_year_amount = float(analogs_by_year[0]["net"])
            two_years_ago_amount = float(analogs_by_year[1]["net"])
            weighted_seasonal = 0.70 * last_year_amount + 0.30 * two_years_ago_amount
        else:
            weighted_seasonal = two_year_average_pre

        # Current recent trend only nudges the seasonal estimate.
        estimate = 0.90 * weighted_seasonal + 0.10 * recent_mean

        analog_amounts = [a["net"] for a in analogs]
        two_year_average = sum(analog_amounts) / len(analog_amounts)

        analogs_by_year = sorted(analogs, key=lambda a: a["year"], reverse=True)
        last_year_match = analogs_by_year[0] if analogs_by_year else None
        two_years_ago_match = analogs_by_year[1] if len(analogs_by_year) > 1 else None

        spread_floor = min(analog_amounts + [estimate])
        spread_ceiling = max(analog_amounts + [estimate])

        # Expand the range slightly because commission checks are inherently variable.
        low = max(0.0, min(spread_floor, estimate * .82))
        high = max(estimate, max(spread_ceiling, estimate * 1.18))

        closest = analogs[0]
        reason_bits = [
            f"closest {closest['year']} check: "
            f"{closest['date'].strftime('%b %d')} ({money(closest['net'])})"
        ]
        if len(analogs) > 1:
            a2 = analogs[1]
            reason_bits.append(
                f"{a2['year']} analog: {a2['date'].strftime('%b %d')} ({money(a2['net'])})"
            )
        if sales_period_label(target_date):
            reason_bits.append(sales_period_label(target_date))

        confidence = (
            "High" if len(analogs) >= 2 and len(hist) >= 12
            else "Medium" if analogs
            else "Low"
        )
        return {
            "estimate": max(0.0, estimate),
            "low": low,
            "high": high,
            "confidence": confidence,
            "reason": " + ".join(reason_bits),
            "samples": len(hist),
            "analogs": analogs,
            "two_year_average": two_year_average,
            "last_year_match": last_year_match,
            "two_years_ago_match": two_years_ago_match,
            "target_check_type": paycheck_cycle_strength(target_date),
        }

    # No calendar analog found: fall back to a conservative recent-history model.
    estimate = recent_mean if recent else float(fallback or 0)
    amounts = sorted(x["net"] for x in hist)
    low = amounts[max(0, int(len(amounts) * .20) - 1)] if amounts else estimate
    high = amounts[min(len(amounts)-1, int(len(amounts) * .80))] if amounts else estimate

    return {
        "estimate": max(0.0, estimate),
        "low": max(0.0, min(low, estimate)),
        "high": max(estimate, high),
        "confidence": "Medium" if len(hist) >= 8 else "Low",
        "reason": "No close prior-year date found; using recent paycheck history",
        "samples": len(hist),
        "analogs": [],
        "two_year_average": None,
        "last_year_match": None,
        "two_years_ago_match": None,
        "target_check_type": paycheck_cycle_strength(target_date),
    }


def dynamic_checking_floor(savings_balance):
    """
    Adaptive liquidity floor:
    when savings is weak, checking must carry more emergency weight;
    when savings is healthy, checking can safely run somewhat lower.
    """
    base = max(250.0, float(BASE["protected_buffer"]))
    emergency = max(
        0.0,
        float(STATE["settings"].get("emergency_savings_floor", 1000) or 0)
    )
    preferred = max(
        emergency + 1.0,
        float(STATE["settings"].get("preferred_savings_floor", 3000) or 0)
    )
    savings_balance = max(0.0, float(savings_balance or 0))

    if savings_balance <= emergency:
        return max(base, base * 1.50)
    if savings_balance >= preferred:
        return max(500.0, base * 0.75)

    progress = (savings_balance - emergency) / (preferred - emergency)
    multiplier = 1.50 - (0.75 * progress)
    return max(500.0, base * multiplier)


def modeled_savings_restore_date(draw_amount, card_bridge=0.0, start_date=None):
    """
    Restore card bridge first, then replenish temporary savings draw using future modeled Free Cash.
    This avoids promising the same future dollar to both debt payoff and savings restoration.
    """
    if start_date is None:
        start_date = BASE["as_of"].date()
    card_remaining = max(0.0, float(card_bridge or 0))
    savings_remaining = max(0.0, float(draw_amount or 0))
    schedule = []
    savings_done = start_date if savings_remaining <= .005 else None

    for row in LIVE_FORECAST:
        d = row["date"].date()
        if d <= start_date:
            continue
        gross_capacity = max(0.0, float(row.get("available", 0) or 0))

        # Keep a reserve instead of treating all "available" dollars as debt-payoff cash.
        reserve = max(500.0, 0.25 * gross_capacity)
        capacity = max(0.0, gross_capacity - reserve)

        if capacity <= .005:
            continue

        debt_use = min(card_remaining, capacity)
        card_remaining -= debt_use
        capacity -= debt_use

        savings_use = min(savings_remaining, capacity)
        savings_remaining -= savings_use
        capacity -= savings_use

        if debt_use > .005 or savings_use > .005:
            schedule.append({
                "date": d,
                "card": debt_use,
                "savings": savings_use,
                "card_remaining": max(0.0, card_remaining),
                "savings_remaining": max(0.0, savings_remaining),
            })

        if savings_remaining <= .005 and savings_done is None:
            savings_done = d
        if card_remaining <= .005 and savings_remaining <= .005:
            break

    return savings_done, schedule


def effective_paycheck(row):
    key = row["date"].date().isoformat()

    posted_actual = actual_paycheck_for_scheduled_date(row["date"])
    if posted_actual:
        return float(posted_actual["net"])

    override = STATE["paychecks"].get(key, {})
    actual = override.get("actual")
    expected = override.get("expected", row["income"])
    manual = bool(override.get("manual", False))

    if actual not in (None, ""):
        return float(actual)

    if manual and expected not in (None, ""):
        return float(expected)

    if STATE["settings"].get("smart_income_enabled", True):
        model = smart_paycheck_estimate(row["date"], fallback=row["income"])
        if model["samples"] >= 4:
            return float(model["estimate"])

    return float(expected if expected not in (None, "") else row["income"])

def live_mtd_spend():
    """
    Month-to-date true spending used by Live Free Cash.

    When Plaid is live, Plaid transactions are the authoritative spending ledger.
    Only true spending is counted; transfers, card payments, refunds, deposits,
    payroll, and other money movement are excluded by the transaction classifier.
    Unsynced Quick Entry purchases are layered on top until Plaid catches them.

    Without Plaid live mode, use the saved Finance OS baseline + Quick Entry behavior.
    """
    as_of = BASE["as_of"].date()

    if plaid_live_balances_enabled():
        total = 0.0
        for tx in STATE.get("plaid", {}).get("transactions", []):
            if tx.get("pending"):
                continue
            try:
                d = datetime.fromisoformat(str(tx.get("date"))).date()
            except Exception:
                continue
            if d.year != as_of.year or d.month != as_of.month or d > as_of:
                continue
            if classify_plaid_transaction(tx) != "true_spending":
                continue
            amount = float(tx.get("amount", 0) or 0)
            if amount > 0:
                total += amount

        # Preserve brand-new manual purchases until they appear in Plaid.
        for tx in unsynced_live_entries():
            if not is_purchase(tx):
                continue
            d = tx_date(tx).date()
            if d.year == as_of.year and d.month == as_of.month and d <= as_of:
                total += float(tx.get("amount", 0) or 0)
        return total

    total = BASE["mtd_spend"]
    for tx in STATE["transactions"]:
        if not is_purchase(tx):
            continue
        d = tx_date(tx)
        if d.date() <= as_of and d.year == as_of.year and d.month == as_of.month:
            total += float(tx.get("amount", 0) or 0)
    return total

def live_current_checking():
    plaid_balance = plaid_mapped_balance("checking")
    if plaid_balance is not None:
        checking = plaid_balance
        tx_source = unsynced_live_entries()
    else:
        checking = BASE["current_checking"] + float(
            STATE["settings"].get("checking_adjustment", 0) or 0
        )
        tx_source = STATE["transactions"]

    for tx in tx_source:
        d = tx_date(tx)
        if d.date() > BASE["as_of"].date():
            continue
        amount = float(tx.get("amount", 0) or 0)
        t = tx.get("type")
        if t == "Income":
            checking += amount
        elif t in {"Expense", "Card payment", "Transfer to savings"}:
            checking -= amount
        elif t == "Transfer from savings":
            checking += amount
        # Card purchases do not immediately reduce checking.
    return checking

def live_free_cash_breakdown():
    """
    Live Finance OS Free Cash formula.

    The formula stays familiar, but every live input is recalculated:
      available checking - protected buffer - spending reserve to next payday.

    In Plaid live mode, checking is Plaid AVAILABLE balance and MTD spending is
    reconstructed from synced true-spending transactions plus unsynced Quick Entry.
    """
    checking = live_current_checking()
    raw_cushion = max(0.0, checking - BASE["protected_buffer"])

    # Use the rolling live forecast rather than the static workbook horizon.
    # The current day is not a future paycheck merely because a legacy row shares
    # today's date.
    future_paydays = [
        r["date"] for r in rolling_forecast_base_rows()
        if r["date"].date() > BASE["as_of"].date()
    ]
    next_payday = min(future_paydays) if future_paydays else BASE["as_of"]
    days_to_payday = max(0, (next_payday.date() - BASE["as_of"].date()).days)

    mtd_spend = live_mtd_spend()
    daily_pace = mtd_spend / max(1, BASE["as_of"].day)
    reserve = daily_pace * days_to_payday * BASE["spend_multiplier"]
    free_cash = max(0.0, raw_cushion - reserve)

    return {
        "checking": checking,
        "protected_buffer": float(BASE["protected_buffer"]),
        "raw_cushion": raw_cushion,
        "mtd_spend": mtd_spend,
        "daily_pace": daily_pace,
        "days_to_payday": days_to_payday,
        "next_payday": next_payday,
        "reserve": reserve,
        "free_cash": free_cash,
    }


def live_free_cash():
    return live_free_cash_breakdown()["free_cash"]

def _forecast_template_for_payday(pay_date):
    """
    Use saved forecast rows as obligation templates for newly generated paydays.
    Prefer the same paycheck position (first/second of month); fall back to
    medians across known rows so the rolling engine never simply stops.
    """
    known = BASE.get("forecast", [])
    if not known:
        return {
            "cash_bills": 0.0, "card_pay": 0.0, "other_planned": 0.0,
            "total_bills": 0.0, "card_funded_bills": 0.0,
        }

    target_bonus = is_first_paycheck_of_month(pay_date)
    same_type = [
        r for r in known
        if is_first_paycheck_of_month(r["date"]) == target_bonus
    ] or known

    def med(field):
        vals = [float(r.get(field, 0) or 0) for r in same_type]
        return float(statistics.median(vals)) if vals else 0.0

    return {
        "cash_bills": med("cash_bills"),
        "card_pay": med("card_pay"),
        "other_planned": med("other_planned"),
        "total_bills": med("total_bills"),
        "card_funded_bills": med("card_funded_bills"),
    }


def rolling_forecast_base_rows(engine_count=None):
    """
    Always maintain a rolling forward horizon.
    Finance OS generates a rolling horizon from the saved paycheck cadence and
    recurring obligation templates.
    """
    engine_count = int(
        engine_count or STATE["settings"].get("forecast_engine_checks", 26) or 26
    )
    today = max(BASE["as_of"].date(), datetime.today().date())
    dates = biweekly_payday_dates(count=engine_count, from_date=today)
    known_map = {r["date"].date(): r for r in BASE.get("forecast", [])}

    rows = []
    for i, d in enumerate(dates):
        if d in known_map:
            row = dict(known_map[d])
            row["generated"] = False
        else:
            template = _forecast_template_for_payday(d)
            next_d = dates[i+1] - timedelta(days=1) if i+1 < len(dates) else d + timedelta(days=13)
            row = {
                "row": None,
                "date": datetime.combine(d, datetime.min.time()),
                "cycle_end": datetime.combine(next_d, datetime.min.time()),
                "income": 0.0,
                "total_bills": template["total_bills"],
                "card_funded_bills": template["card_funded_bills"],
                "cash_bills": template["cash_bills"],
                "card_pay": template["card_pay"],
                "other_planned": template["other_planned"],
                "start_checking": 0.0,
                "before_savings": 0.0,
                "savings_deposit": 0.0,
                "checking": 0.0,
                "savings": 0.0,
                "available": 0.0,
                "generated": True,
            }
        rows.append(row)

    # Normalize cycle ends to the day before the next generated paycheck.
    for i, row in enumerate(rows):
        if i+1 < len(rows):
            row["cycle_end"] = rows[i+1]["date"] - timedelta(days=1)
        elif row.get("cycle_end") is None:
            row["cycle_end"] = row["date"] + timedelta(days=13)

    return rows


def cycle_transactions(row):
    start = row["date"]
    end = row["cycle_end"]
    return [tx for tx in STATE["transactions"] if start <= tx_date(tx) <= end]

def adjusted_forecast():
    # Recalculate each payday sequentially from the same columns the workbook uses:
    # I + C - F - G - H = J ; K = savings rule ; L = J-K ; M accumulates savings ;
    # N = max(0, L-buffer-Quick Entry purchases in cycle).
    rows = []
    prev_checking = BASE["starting_checking"]
    prev_savings = BASE["starting_savings"]

    rolling_base = rolling_forecast_base_rows()
    for idx, base_row in enumerate(rolling_base):
        if idx == 0:
            prev_checking = live_current_checking()

        income = effective_paycheck(base_row)
        cash_bills = base_row["cash_bills"]
        card_pay = base_row["card_pay"]
        other_planned = base_row["other_planned"]

        extra_cash_expense = 0.0
        extra_income = 0.0
        extra_card_payment = 0.0
        direct_savings_transfer = 0.0
        qualifying_purchase_spend = 0.0

        for tx in cycle_transactions(base_row):
            amount = float(tx.get("amount", 0) or 0)
            t = tx.get("type")
            if t == "Income":
                extra_income += amount
            elif t == "Expense":
                extra_cash_expense += amount
                qualifying_purchase_spend += amount
            elif t == "Card purchase":
                qualifying_purchase_spend += amount
            elif t == "Card payment":
                extra_card_payment += amount
            elif t == "Transfer to savings":
                direct_savings_transfer += amount

        # Confirmed recurring non-payroll income (e.g. roommate rent contribution)
        # is forecast separately from payroll and never contaminates paycheck averages.
        cycle_start = base_row["date"].date()
        cycle_end_raw = base_row.get("cycle_end", base_row["date"])
        cycle_end = cycle_end_raw.date() if isinstance(cycle_end_raw, datetime) else cycle_end_raw
        recurring_income_rows = expected_recurring_income_between(cycle_start, cycle_end)
        recurring_income = sum(float(x["amount"]) for x in recurring_income_rows)

        before_savings = (
            prev_checking + income + recurring_income + extra_income
            - cash_bills - extra_cash_expense
            - card_pay - extra_card_payment
            - other_planned
        )

        extra_savings = 0.0
        extra_date = BASE["extra_savings_date"]
        if isinstance(extra_date, date):
            exd = extra_date if isinstance(extra_date, datetime) else datetime.combine(extra_date, datetime.min.time())
            if base_row["date"].date() == exd.date():
                extra_savings = BASE["extra_savings_amount"]

        # Dynamic savings capacity: save aggressively only after preserving the adaptive
        # liquidity floor. This prevents a rigid percentage transfer from making a tight
        # paycheck unnecessarily fragile, while allowing stronger checks to catch up.
        adaptive_floor = dynamic_checking_floor(prev_savings)
        target_auto_savings = max(0, before_savings * BASE["savings_rate"]) + extra_savings
        safe_savings_capacity = max(0.0, before_savings - adaptive_floor - qualifying_purchase_spend)
        auto_savings = min(target_auto_savings, safe_savings_capacity)
        deferred_savings = max(0.0, target_auto_savings - auto_savings)
        ending_checking = before_savings - auto_savings - direct_savings_transfer
        ending_savings = prev_savings + auto_savings + direct_savings_transfer
        available = max(0, ending_checking - adaptive_floor - qualifying_purchase_spend)

        rows.append({
            **base_row,
            "income": income,
            "recurring_income": recurring_income,
            "recurring_income_rows": recurring_income_rows,
            "start_checking": prev_checking,
            "cash_bills": cash_bills + extra_cash_expense,
            "card_pay": card_pay + extra_card_payment,
            "other_planned": other_planned,
            "before_savings": before_savings,
            "savings_deposit": auto_savings + direct_savings_transfer,
            "target_savings_deposit": target_auto_savings + direct_savings_transfer,
            "deferred_savings": deferred_savings,
            "adaptive_floor": adaptive_floor,
            "checking": ending_checking,
            "savings": ending_savings,
            "available": available,
            "quick_entry_spend": qualifying_purchase_spend,
        })

        prev_checking = ending_checking
        prev_savings = ending_savings

    return rows

LIVE_FORECAST = adjusted_forecast()
LIVE_CARDS = live_card_rows()

def projected_month_end():
    month_rows = [
        row for row in LIVE_FORECAST
        if row["date"].year == BASE["as_of"].year and row["date"].month == BASE["as_of"].month
    ]
    return month_rows[-1]["checking"] if month_rows else live_current_checking()

def next_comfortable():
    for row in LIVE_FORECAST:
        if row["date"] >= BASE["as_of"] and row["available"] >= 500:
            return row["date"]
    return None

def horizon_stats(days):
    rows = [
        row for row in LIVE_FORECAST
        if 0 <= (row["date"] - BASE["as_of"]).days <= days
    ]
    if not rows:
        rows = LIVE_FORECAST[:1]
    if not rows:
        return {"low":0, "payments":0, "end":0, "end_available":0, "status":"WATCH"}

    low = min(row["checking"] for row in rows)
    payments = sum(row["card_pay"] for row in rows)
    end = rows[-1]["checking"]
    end_available = rows[-1]["available"]
    if days == 30 and live_free_cash() < 500:
        status = "TIGHT"
    elif end_available >= 1000:
        status = "COMFORTABLE"
    elif end_available >= 500:
        status = "RECOVERING"
    else:
        status = "TIGHT"
    return {"low":low,"payments":payments,"end":end,"end_available":end_available,"status":status}

def save_and_reload(message):
    try:
        if st.session_state.get("fos_v4_mode") in {"demo", "personal_preview"}:
            st.session_state.live_state = STATE
            st.toast("Updated for this test session")
            st.rerun()
        mode = save_state(STATE, message=message)
        st.session_state.live_state = STATE
        if mode == "github":
            st.success("Saved permanently.")
        else:
            st.warning(
                "Saved in local app storage. Streamlit Cloud can erase local changes after a restart. "
                "Configure the GitHub secret for permanent saves."
            )
        st.rerun()
    except Exception as exc:
        st.error(f"Save failed: {exc}")

# ---------- Recovery / scenario helpers ----------

def incremental_card_debt():
    """Net new card debt created in live state relative to workbook baseline."""
    debt = 0.0
    for tx in STATE["transactions"]:
        amount = float(tx.get("amount", 0) or 0)
        if tx.get("type") == "Card purchase":
            debt += amount
        elif tx.get("type") == "Card payment":
            debt -= amount
    return max(0.0, debt)


def modeled_debt_catchup_date(amount=None, start_date=None):
    """First payday where cumulative modeled free cash can absorb incremental debt."""
    remaining = incremental_card_debt() if amount is None else max(0.0, float(amount))
    if remaining <= 0.005:
        return BASE["as_of"].date(), []
    if start_date is None:
        start_date = BASE["as_of"].date()
    schedule = []
    for row in LIVE_FORECAST:
        d = row["date"].date()
        if d < start_date:
            continue
        capacity = max(0.0, float(row["available"]))
        if capacity <= 0:
            continue
        payment = min(remaining, capacity)
        remaining -= payment
        schedule.append({"date": d, "payment": payment, "remaining": max(0.0, remaining)})
        if remaining <= 0.005:
            return d, schedule
    return None, schedule



def modeled_plan_recovery_date(impact_amount, start_date=None):
    """
    Estimate when a scenario's total financial disruption is absorbed by future modeled Free Cash.

    Unlike modeled_debt_catchup_date(), this is not limited to card debt. A cash purchase still
    pushes checking below the baseline plan, so it must recover through future surplus cash.

    The expense date itself is excluded from recovery capacity because those dollars are being
    spent in the scenario now; only later forecast cycles can restore the plan.
    """
    remaining = max(0.0, float(impact_amount or 0))
    if remaining <= 0.005:
        return start_date or BASE["as_of"].date(), []

    if start_date is None:
        start_date = BASE["as_of"].date()

    schedule = []
    for row in LIVE_FORECAST:
        d = row["date"].date()

        # Recovery must happen AFTER the scenario date.
        if d <= start_date:
            continue

        capacity = max(0.0, float(row.get("available", 0) or 0))
        if capacity <= 0.005:
            continue

        absorbed = min(remaining, capacity)
        remaining -= absorbed
        schedule.append({
            "date": d,
            "payment": absorbed,
            "remaining": max(0.0, remaining),
        })

        if remaining <= 0.005:
            return d, schedule

    return None, schedule


def projected_goal_date(goal):
    target = float(goal.get("target", 0) or 0)
    current = float(goal.get("current", 0) or 0)
    if target <= 0 or current >= target:
        return BASE["as_of"].date()
    # Goals are treated as savings-backed until richer goal account metadata is added.
    for row in LIVE_FORECAST:
        if float(row["savings"]) >= target:
            return row["date"].date()
    return None


def compact_recovery_summary():
    parts = []
    debt = incremental_card_debt()
    if debt > 0:
        catchup, _ = modeled_debt_catchup_date(debt)
        if catchup:
            parts.append(f"Cards caught up by {catchup.strftime('%b %d')} expected")
        else:
            parts.append(f"{money(debt)} incremental card debt remains beyond forecast")
    else:
        parts.append("No incremental card debt")

    if STATE.get("goals"):
        goal = STATE["goals"][0]
        gd = projected_goal_date(goal)
        if gd:
            parts.append(f"{goal['name']} goal projected by {gd.strftime('%b %d')}")
        else:
            parts.append(f"{goal['name']} goal not reached in current forecast")
    return " • ".join(parts)



def finance_today():
    """Use the later of workbook as-of and actual system day for rolling UI decisions."""
    return max(BASE["as_of"].date(), datetime.today().date())


def due_date_for_day(due_day, reference=None, allow_today=True):
    reference = reference or finance_today()
    due_day = max(1, min(31, int(due_day)))

    def valid_date(year, month, day):
        while day >= 1:
            try:
                return date(year, month, day)
            except ValueError:
                day -= 1
        return date(year, month, 1)

    d = valid_date(reference.year, reference.month, due_day)
    if d < reference or (d == reference and not allow_today):
        y, m = reference.year, reference.month + 1
        if m == 13:
            y, m = y + 1, 1
        d = valid_date(y, m, due_day)
    return d


def next_payday_after(reference=None):
    reference = reference or finance_today()
    dates = [
        r["date"].date() for r in rolling_forecast_base_rows()
        if r["date"].date() > reference
    ]
    return min(dates) if dates else reference + timedelta(days=14)


def recurring_obligations_between(start_date, end_date):
    """
    Return known, itemized Finance OS obligations whose next due occurrence falls
    inside the requested window. These live in persistent user state, not Excel.
    """
    rows = []
    for bill in STATE.get("settings", {}).get("recurring_bills", []):
        if not bill.get("active", False):
            continue
        due = due_date_for_day(bill["due_day"], start_date, allow_today=True)
        if start_date <= due <= end_date:
            rows.append({
                **bill,
                "due_date": due,
                "source": "Finance OS recurring bill",
            })

    # Future explicit Quick Entry cash/card-payment items are also real obligations.
    for tx in STATE.get("transactions", []):
        try:
            d = tx_date(tx).date()
        except Exception:
            continue
        if not (start_date <= d <= end_date):
            continue
        if tx.get("type") not in {"Expense", "Card payment"}:
            continue
        rows.append({
            "id": f"quick-{tx.get('id','')}",
            "name": tx.get("note") or tx.get("category") or tx.get("type"),
            "due_day": d.day,
            "amount": float(tx.get("amount", 0) or 0),
            "payment_method": (
                tx.get("card") or "Checking"
                if tx.get("type") == "Card payment"
                else "Checking"
            ),
            "category": tx.get("category") or tx.get("type"),
            "active": True,
            "due_date": d,
            "source": "Quick Entry",
        })

    # De-duplicate exact same named/date/amount rows.
    unique = {}
    for row in rows:
        key = (
            str(row.get("name","")).strip().lower(),
            row["due_date"].isoformat(),
            round(float(row.get("amount",0) or 0), 2),
        )
        unique[key] = row
    return sorted(unique.values(), key=lambda x: (x["due_date"], x["name"]))


def card_for_payment_method(method):
    clean = re.sub(r"[^A-Z0-9]", "", str(method or "").upper())
    if clean in {"", "CHECKING", "CASH"}:
        return None
    for card in LIVE_CARDS:
        cclean = re.sub(r"[^A-Z0-9]", "", str(card.get("card","")).upper())
        if clean == cclean or clean in cclean or cclean in clean:
            return card
    return None


def obligation_funding_status(row, cumulative_checking_due=0.0):
    method = str(row.get("payment_method") or "Checking")
    amount = max(0.0, float(row.get("amount", 0) or 0))
    if method.strip().lower() in {"checking","cash"}:
        floor = dynamic_checking_floor(live_current_savings())
        funded = live_current_checking() - cumulative_checking_due - amount >= floor
        return "FUNDED" if funded else "NEEDS CASH"

    card = card_for_payment_method(method)
    if not card:
        return "PLANNED"
    if not card.get("limit_known"):
        return "CARD • LIMIT UNKNOWN"
    remaining_credit = max(0.0, float(card.get("limit",0)) - float(card.get("balance",0)))
    return "CARD FUNDED" if remaining_credit + .01 >= amount else "CARD TIGHT"


def weekly_command_rows(days=7):
    today = finance_today()
    end = today + timedelta(days=max(0, int(days)))
    rows = recurring_obligations_between(today, end)
    running_checking = 0.0
    result = []
    for row in rows:
        method = str(row.get("payment_method") or "Checking")
        status = obligation_funding_status(row, running_checking)
        if method.strip().lower() in {"checking","cash"}:
            running_checking += max(0.0, float(row.get("amount",0) or 0))
        result.append({**row, "status": status})
    return result


def latest_paycheck_context():
    """
    Prefer a verified Plaid payroll deposit from the last 4 days. If today is a
    scheduled payday but Plaid has not classified the deposit yet, use the planned
    paycheck and clearly label it expected rather than received.
    """
    today = finance_today()
    try:
        detected = plaid_detected_paychecks()
    except Exception:
        detected = []

    recent = [
        x for x in detected
        if 0 <= (today - x["date"]).days <= 4
    ]
    if recent:
        x = sorted(recent, key=lambda z: z["date"])[-1]
        return {
            "date": x["date"],
            "amount": float(x["net"]),
            "verified": True,
            "label": "Verified payroll deposit",
        }

    for row in rolling_forecast_base_rows():
        d = row["date"].date()
        if d == today:
            return {
                "date": d,
                "amount": float(effective_paycheck(row)),
                "verified": False,
                "label": "Scheduled paycheck",
            }
    return None


def payday_command_plan():
    """
    Explain what current cash needs to accomplish before the next payday.
    This intentionally avoids pretending every dollar in checking came from the
    most recent paycheck; it plans the user's entire current cash position.
    """
    today = finance_today()
    next_pay = next_payday_after(today)
    obligations = recurring_obligations_between(today, next_pay - timedelta(days=1))

    checking_due = sum(
        float(x.get("amount",0) or 0)
        for x in obligations
        if str(x.get("payment_method","")).lower() in {"checking","cash"}
    )
    card_charges_due = sum(
        float(x.get("amount",0) or 0)
        for x in obligations
        if str(x.get("payment_method","")).lower() not in {"checking","cash"}
    )

    checking = live_current_checking()
    savings = live_current_savings()
    floor = dynamic_checking_floor(savings)
    free_detail = live_free_cash_breakdown()
    variable_reserve = max(0.0, float(free_detail.get("reserve", 0) or 0))

    card_payment_now = sum(
        max(0.0, float(p.get("recommended_payment",0) or 0))
        for p in OVERVIEW_CARD_PLANS
    )

    # Don't reserve the same dollars twice. Safe-to-spend is the smaller of the
    # live Free Cash engine and the transparent paycheck-window cash calculation.
    transparent_room = max(
        0.0,
        checking - checking_due - floor - variable_reserve - card_payment_now,
    )
    safe_to_spend = min(max(0.0, live_free_cash()), transparent_room)

    return {
        "today": today,
        "next_payday": next_pay,
        "checking": checking,
        "savings": savings,
        "checking_bills": checking_due,
        "card_charges": card_charges_due,
        "protected_cash": floor,
        "variable_reserve": variable_reserve,
        "recommended_card_payments": card_payment_now,
        "safe_to_spend": safe_to_spend,
        "obligations": obligations,
        "paycheck": latest_paycheck_context(),
    }


def next_best_move_v2(plan=None):
    plan = plan or payday_command_plan()
    cards = [
        p for p in OVERVIEW_CARD_PLANS
        if float(p.get("recommended_payment",0) or 0) > .005
    ]
    if cards:
        card = max(cards, key=lambda p: float(p.get("recommended_payment",0) or 0))
        return {
            "title": f"Pay {card['card']} {money(card['recommended_payment'])}",
            "why": card.get("advice") or "This payment is supported by current genuinely free cash.",
        }
    if plan["checking_bills"] > 0:
        return {
            "title": f"Reserve {money(plan['checking_bills'])} for checking bills",
            "why": f"Those obligations arrive before the next payday on {plan['next_payday'].strftime('%b %d')}.",
        }
    return {
        "title": "No money move needed today",
        "why": "Known obligations and protected cash are covered. Keep following the current plan.",
    }


def spending_summary_v2():
    intel = spending_intelligence()
    if not intel:
        return None
    cats = intel.get("categories", []) or []
    increases = sorted(cats, key=lambda x: float(x.get("Change / mo",0) or 0), reverse=True)
    decreases = sorted(cats, key=lambda x: float(x.get("Change / mo",0) or 0))
    return {
        "recent_monthly": float(intel.get("recent_monthly",0) or 0),
        "prior_monthly": float(intel.get("prior_monthly",0) or 0),
        "delta": float(intel.get("recent_monthly",0) or 0) - float(intel.get("prior_monthly",0) or 0),
        "increases": [x for x in increases if float(x.get("Change / mo",0) or 0) > 0][:4],
        "decreases": [x for x in decreases if float(x.get("Change / mo",0) or 0) < 0][:4],
        "raw": intel,
    }



def overview_card_plan():
    """
    Simple, auditable card plan.

    This planner intentionally does NOT invent tiny future payments or distant payoff
    dates from generic forecast surplus. It answers only:
      - what is due,
      - whether the balance is high utilization,
      - whether cash is genuinely available now,
      - whether a full payoff is safe now.

    Future payoff dates are shown only when an explicit modeled payment schedule
    actually covers the full current balance.
    """
    checking = live_current_checking()
    savings = live_current_savings()
    floor = dynamic_checking_floor(savings)
    safe_cash_now = max(0.0, checking - floor)
    free_cash_now = max(0.0, live_free_cash())

    def next_due(raw):
        if raw is None:
            return None
        if isinstance(raw, datetime):
            d = raw.date()
        elif isinstance(raw, date):
            d = raw
        elif isinstance(raw, (int, float)) and 1 <= raw <= 31:
            try:
                d = date(BASE["as_of"].year, BASE["as_of"].month, int(raw))
            except Exception:
                return None
        else:
            s = str(raw).strip()
            try:
                d = date(BASE["as_of"].year, BASE["as_of"].month, int(float(s)))
            except Exception:
                try:
                    d = datetime.fromisoformat(s).date()
                except Exception:
                    return None
        if d < BASE["as_of"].date():
            y, m = d.year, d.month + 1
            if m == 13:
                y, m = y + 1, 1
            day = d.day
            while day:
                try:
                    d = date(y, m, day)
                    break
                except ValueError:
                    day -= 1
        return d

    # Allocate only genuinely safe cash that exists NOW. Do not manufacture a
    # November/December micro-payment plan from future forecast rows.
    immediate_pool = min(safe_cash_now, free_cash_now)
    plans = []

    ordered = sorted(
        LIVE_CARDS,
        key=lambda c: (
            -float(c.get("util", 0) or 0),
            -float(c.get("balance", 0) or 0),
        )
    )

    allocations = {}
    for card in ordered:
        bal = max(0.0, float(card.get("balance", 0) or 0))
        limit_known = bool(card.get("limit_known", False)) and float(card.get("limit", 0) or 0) > 1
        lim = float(card.get("limit", 0) or 0) if limit_known else 0.0
        util = bal / lim if limit_known and lim else None

        # Only recommend a current extra payment when it meaningfully helps.
        pay = 0.0
        if bal > .005 and immediate_pool > .005:
            if bal <= immediate_pool:
                pay = bal
            elif util is not None and util >= .90:
                target = max(0.0, bal - .70 * lim)
                pay = min(immediate_pool, target)
            elif util is not None and util >= .70:
                target = max(0.0, bal - .50 * lim)
                pay = min(immediate_pool, target)
        allocations[card["card"]] = max(0.0, pay)
        immediate_pool -= max(0.0, pay)

    for card in LIVE_CARDS:
        bal = max(0.0, float(card.get("balance", 0) or 0))
        limit_known = bool(card.get("limit_known", False)) and float(card.get("limit", 0) or 0) > 1
        lim = float(card.get("limit", 0) or 0) if limit_known else 0.0
        util = bal / lim if limit_known and lim else None
        due = next_due(card.get("due"))
        pay_now = allocations.get(card["card"], 0.0)
        remaining = max(0.0, bal - pay_now)
        due_util = remaining / lim if limit_known and lim else None

        if bal <= .005:
            status = "CLEAR"
            headline = "No balance to address"
            advice = "No payment action needed."
        elif pay_now >= bal - .01:
            status = "PAY IN FULL"
            headline = f"Pay {money(bal)} in full now"
            advice = (
                f"Finance OS shows enough genuinely free cash to clear this balance "
                f"while preserving the adaptive checking floor of {money(floor)}."
            )
        elif pay_now > .005:
            status = "PAY DOWN NOW"
            headline = f"Pay {money(pay_now)} now"
            advice = (
                f"This is a meaningful utilization-reduction payment supported by current "
                f"free cash. Remaining balance would be about {money(remaining)}. "
                "No future payoff date is promised until the forecast contains an explicit safe payoff."
            )
        elif util is not None and util >= .50:
            status = "HIGH UTILIZATION"
            headline = "High utilization — protect cash, then attack balance"
            advice = (
                f"Current utilization is {util:.1%}. Finance OS does not see enough genuinely "
                "free cash today for a responsible extra payment. Keep required minimum/autopay "
                "current and reassess after the next verified paycheck."
            )
        else:
            status = "MONITOR"
            headline = "Keep required payment current"
            advice = (
                "No extra payment is recommended from today's protected cash. "
                "Reassess after the next verified paycheck."
            )

        plans.append({
            **card,
            "due_date": due,
            "due_util": due_util,
            "payoff_date": BASE["as_of"].date() if pay_now >= bal-.01 and bal > .005 else None,
            "status": status,
            "priority_tier": (
                "HIGH" if util is not None and util >= .50 else
                "MEDIUM" if util is not None and util >= .30 else "LOW"
            ),
            "headline": headline,
            "next_payment": (
                {"date": BASE["as_of"].date(), "payment": pay_now, "kind": "safe cash now"}
                if pay_now > .005 else None
            ),
            "recommended_payment": pay_now,
            "recommended_carry": remaining,
            "advice": advice,
            "payment_schedule": (
                [{"date": BASE["as_of"].date(), "payment": pay_now, "kind": "safe cash now"}]
                if pay_now > .005 else []
            ),
            "liquidity_floor_used": floor,
        })
    return plans

OVERVIEW_CARD_PLANS = overview_card_plan()



def finance_ai_task_prompt(task, context=None):
    context = context or {}
    prompts = {
        "daily_brief": (
            "Give me today's financial brief. Tell me the single best move first, "
            "then 2-3 things I should know today. Be concise and actionable."
        ),
        "payday": (
            "Create a payday allocation plan for the next paycheck. State how much "
            "should remain in checking, how much should go to each card or debt, how "
            "much should go to savings, and how much is genuinely discretionary. "
            "Do not allocate more cash than the deterministic forecast supports."
        ),
        "card": (
            f"Review the card {context.get('card','')} specifically. Explain whether "
            "I should pay it in full, partially pay it, or temporarily carry it, and why."
        ),
        "free_cash": (
            "Explain my Live Free Cash in plain English: why it is at its current "
            "level, what is constraining it, and what event will materially improve it."
        ),
        "purchase": (
            f"Evaluate a purchase of ${float(context.get('amount',0) or 0):,.2f}. "
            "Compare buying now versus waiting for a healthier forecast point. "
            "Separate affordability from whether buying now is financially smart."
        ),
        "audit": (
            "Audit Finance OS's deterministic plan. Identify any recommendation that "
            "looks too conservative, too aggressive, internally inconsistent, or "
            "unsupported by the supplied data. Then state your preferred plan."
        ),
        "monthly": (
            "Give me a concise monthly financial review: what improved, what worsened, "
            "the biggest risk, and the three highest-impact moves from here."
        ),
    }
    return prompts.get(task, prompts["daily_brief"])


def deterministic_action_center():
    """
    Rank a few useful actions using deterministic Finance OS facts.
    AI explains these actions; it does not create them.
    """
    actions = []
    checking = live_current_checking()
    savings = live_current_savings()
    floor = dynamic_checking_floor(savings)
    free = live_free_cash()
    surplus = max(0.0, checking - floor)

    # Credit action.
    payable = [
        p for p in OVERVIEW_CARD_PLANS
        if float(p.get("balance", 0) or 0) > 0
    ]
    payable.sort(
        key=lambda p: (
            0 if p.get("status") == "PAY IN FULL" else 1,
            -float(p.get("util", 0) or 0),
            -float(p.get("balance", 0) or 0),
        )
    )
    if payable:
        p = payable[0]
        recommended = float(p.get("recommended_payment", 0) or 0)
        if recommended > 0:
            actions.append({
                "priority": 1,
                "title": f"Pay {p.get('card','card')} {money(recommended)}",
                "tag": "Safe now" if recommended <= surplus + 0.01 else "Planned",
                "why": str(p.get("advice") or p.get("headline") or "Reduces card pressure while protecting liquidity."),
                "card": p.get("card"),
            })

    # Liquidity action.
    actions.append({
        "priority": 2,
        "title": f"Protect {money(floor)} in checking",
        "tag": "Required protection",
        "why": (
            f"Your adaptive cash floor is {money(floor)}. Finance OS treats cash above "
            "this floor as potentially deployable only after upcoming obligations remain covered."
        ),
    })

    # Savings / waiting action.
    next_good = next_comfortable()
    if free < 500 and next_good:
        actions.append({
            "priority": 3,
            "title": f"Hold extra spending until {fmt_date(next_good)}",
            "tag": "Wait",
            "why": (
                f"Live Free Cash is {money(free)} today. The forecast reaches a more "
                "comfortable discretionary position around this date."
            ),
        })
    elif surplus > 0:
        actions.append({
            "priority": 3,
            "title": f"Review {money(surplus)} of cash above floor",
            "tag": "Optimize",
            "why": (
                "This is cash above the adaptive checking floor. Use the forecast and "
                "card plan before deciding whether it belongs in debt payoff or savings."
            ),
        })

    return actions[:3]


def ai_review_cache_key(snapshot, question):
    raw = json.dumps(
        {"snapshot": snapshot, "question": question},
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def run_or_reuse_finance_ai(snapshot, question):
    """
    Avoid paying for the same review repeatedly when neither the question nor
    financial snapshot changed.
    """
    key = ai_review_cache_key(snapshot, question)
    cache = st.session_state.setdefault("finance_ai_review_cache", {})
    if key in cache:
        result = dict(cache[key])
        result["cached"] = True
        return result

    result = call_finance_ai(snapshot, question)
    result["cached"] = False
    cache[key] = dict(result)

    # Keep only a handful of recent reviews in session memory.
    if len(cache) > 12:
        oldest = next(iter(cache))
        cache.pop(oldest, None)
    return result



def finance_ai_snapshot():
    """
    Build a sanitized, decision-ready snapshot for AI review.

    No Plaid access tokens, account IDs, transaction IDs, routing/account numbers,
    GitHub credentials, OpenAI keys, or raw bank-auth data are included.
    """
    free = live_free_cash_breakdown()
    checking = live_current_checking()
    savings = live_current_savings()
    adaptive_floor = dynamic_checking_floor(savings)

    forecast_rows = []
    for row in LIVE_FORECAST[:8]:
        forecast_rows.append({
            "payday": row["date"].date().isoformat(),
            "income": round(float(row.get("income", 0) or 0), 2),
            "cash_bills": round(float(row.get("cash_bills", 0) or 0), 2),
            "card_payments": round(float(row.get("card_pay", 0) or 0), 2),
            "other_planned": round(float(row.get("other_planned", 0) or 0), 2),
            "ending_checking": round(float(row.get("checking", 0) or 0), 2),
            "ending_savings": round(float(row.get("savings", 0) or 0), 2),
            "surplus_available": round(float(row.get("available", 0) or 0), 2),
        })

    cards = []
    for plan in OVERVIEW_CARD_PLANS:
        limit_known = bool(plan.get("limit_known", False))
        cards.append({
            "name": str(plan.get("card", "Card")),
            "balance": round(float(plan.get("balance", 0) or 0), 2),
            "credit_limit": (
                round(float(plan.get("limit", 0) or 0), 2)
                if limit_known else None
            ),
            "utilization_pct": (
                round(float(plan.get("util", 0) or 0) * 100, 1)
                if limit_known else None
            ),
            "due_date": (
                plan["due_date"].isoformat()
                if isinstance(plan.get("due_date"), date) else
                str(plan.get("due_date") or "")
            ),
            "planner_status": plan.get("status"),
            "planner_headline": plan.get("headline"),
            "recommended_next_payment": round(
                float(plan.get("recommended_payment", 0) or 0), 2
            ),
            "recommended_temporary_carry": round(
                float(plan.get("recommended_carry", 0) or 0), 2
            ),
            "modeled_payoff_date": (
                plan["payoff_date"].isoformat()
                if isinstance(plan.get("payoff_date"), date) else None
            ),
            "planner_advice": str(plan.get("advice", "")),
        })

    h30 = horizon_stats(30)
    h60 = horizon_stats(60)
    h90 = horizon_stats(90)

    recurring = []
    try:
        for item in inferred_recurring_outflows()[:20]:
            recurring.append({
                "merchant": item.get("Merchant"),
                "classification": item.get("Classification"),
                "cadence": item.get("Cadence"),
                "typical_amount": round(float(item.get("Typical amount", 0) or 0), 2),
                "monthly_equivalent": round(float(item.get("Monthly equivalent", 0) or 0), 2),
                "last_seen": str(item.get("Last seen") or ""),
            })
    except Exception:
        recurring = []

    return {
        "as_of_date": BASE["as_of"].date().isoformat(),
        "balance_source": transaction_source_label(),
        "cash_position": {
            "checking_available": round(checking, 2),
            "savings": round(savings, 2),
            "live_free_cash": round(float(free["free_cash"]), 2),
            "protected_buffer": round(float(BASE["protected_buffer"]), 2),
            "adaptive_checking_floor": round(float(adaptive_floor), 2),
            "mtd_true_spending": round(float(free["mtd_spend"]), 2),
            "estimated_spending_reserve_to_next_payday": round(float(free["reserve"]), 2),
            "next_payday": (
                free["next_payday"].date().isoformat()
                if isinstance(free.get("next_payday"), datetime) else
                str(free.get("next_payday") or "")
            ),
        },
        "forecast_30_60_90": {
            "30_days": h30,
            "60_days": h60,
            "90_days": h90,
        },
        "next_8_paychecks": forecast_rows,
        "credit_cards": cards,
        "known_recurring_obligations": recurring,
        "finance_os_priority_order": [
            "Survive",
            "Preserve",
            "Recover",
            "Optimize",
        ],
    }


def finance_ai_snapshot_key(snapshot):
    encoded = json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def card_utilization_milestones(plan):
    """
    Project when the modeled payment schedule brings a card below major utilization
    thresholds. This makes an intentional high-utilization bridge visible and finite.
    """
    balance = max(0.0, float(plan.get("balance", 0) or 0))
    limit = max(.01, float(plan.get("limit", 0) or .01))
    schedule = sorted(plan.get("payment_schedule", []) or [], key=lambda x: x["date"])

    thresholds = [1.00, .90, .70, .50, .30]
    result = {}
    current = balance

    for threshold in thresholds:
        if current / limit <= threshold + 1e-9:
            result[threshold] = BASE["as_of"].date()

    for p in schedule:
        current = max(0.0, current - float(p.get("payment", 0) or 0))
        util = current / limit
        for threshold in thresholds:
            if threshold not in result and util <= threshold + 1e-9:
                result[threshold] = p["date"]

    return result


def active_credit_bridges():
    bridges = []
    for p in OVERVIEW_CARD_PLANS:
        # High utilization is considered an intentional bridge when the model carries
        # a material balance instead of immediately clearing it.
        if p.get("recommended_carry", 0) > .005 and p.get("util", 0) >= .70:
            milestones = card_utilization_milestones(p)
            bridges.append({
                "card": p["card"],
                "util": p["util"],
                "carry": p["recommended_carry"],
                "payoff_date": p.get("payoff_date"),
                "milestones": milestones,
            })
    return sorted(bridges, key=lambda x: -x["util"])



# ---------- Styling ----------

st.markdown("""
<style>
:root{
  --bg:#07111c;--panel:#0d1824;--panel2:#111f2d;--line:#203241;
  --text:#f4f8fb;--muted:#8fa4b8;--blue:#3bb8ff;--green:#55dfa3;
  --amber:#ffc84c;--red:#ff6f6f;--purple:#a889ff;
}
html,body,[data-testid="stAppViewContainer"],[data-testid="stHeader"]{background:var(--bg)}
[data-testid="stAppViewContainer"]>.main{
  background:radial-gradient(circle at 20% 0%,rgba(42,130,200,.11),transparent 30%),
  linear-gradient(180deg,#07111c,#08131f)
}
.block-container{max-width:1380px;padding-top:1.1rem;padding-bottom:3rem}
#MainMenu,footer{visibility:hidden} header{background:transparent!important}

.fo-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.fo-brand{display:flex;gap:12px;align-items:center}
.fo-logo{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,#37c1ff,#7d6cff);display:grid;place-items:center;font-weight:900}
.fo-title{font-size:1.55rem;font-weight:850}.fo-sub,.fo-date{font-size:.78rem;color:var(--muted)}

div[role="radiogroup"]{background:#0b1722;border:1px solid var(--line);border-radius:14px;padding:6px 10px;margin-bottom:16px;gap:5px}
div[role="radiogroup"] label{padding:4px 7px;border-radius:9px}

.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:14px}
.two-grid{display:grid;grid-template-columns:1.7fr 1fr;gap:14px;margin-bottom:14px}
.forecast-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px}
.bottom-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}

.fo-card,.page-card{background:linear-gradient(180deg,#122130,#0b1723);border:1px solid var(--line);border-radius:18px;padding:18px;min-width:0}
.k-label{font-size:.7rem;font-weight:850;letter-spacing:.08em;color:var(--muted);text-transform:uppercase}
.k-value{font-size:1.85rem;font-weight:900;margin-top:7px}
.k-meta{font-size:.78rem;color:var(--muted);margin-top:4px}
.good{color:var(--green)!important}.warn{color:var(--amber)!important}.bad{color:var(--red)!important}

.score-wrap{display:flex;gap:14px;align-items:center;margin-top:8px}
.score-ring{width:72px;height:72px;border-radius:50%;position:relative;background:conic-gradient(var(--amber) calc(var(--p)*1%),#243544 0)}
.score-ring:after{content:"";position:absolute;inset:8px;background:var(--panel2);border-radius:50%}
.score-num{position:absolute;inset:0;z-index:2;display:grid;place-items:center;font-weight:900}

.insight{display:flex;gap:10px;margin-top:11px;font-size:.9rem;line-height:1.45}
.dot{width:8px;height:8px;border-radius:50%;background:var(--blue);margin-top:6px;flex:0 0 auto}
.action-box{border-left:3px solid #5cd8ff;padding-left:14px;margin-top:12px}
.action-title{font-weight:900;color:#7ed9ff}.action-copy{margin-top:7px}
.section-title{font-size:.76rem;font-weight:850;letter-spacing:.11em;text-transform:uppercase;color:var(--muted);margin:4px 0 10px}

.forecast-head{display:flex;justify-content:space-between;align-items:center}
.badge{padding:4px 8px;border-radius:999px;border:1px solid currentColor;font-size:.66rem;font-weight:850}
.metric-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.mini-label{font-size:.69rem;color:var(--muted)}.mini-value{font-size:1.05rem;font-weight:850;margin-top:3px}

.alert{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.06)}
.alert:last-child{border-bottom:none}.alert-main{font-size:.85rem}.alert-meta{font-size:.72rem;color:var(--muted)}

.card-row{margin-top:11px}.card-top{display:flex;justify-content:space-between;font-size:.8rem}
.track{height:7px;background:#1a2a38;border-radius:999px;overflow:hidden;margin-top:6px}.fill{height:100%;background:linear-gradient(90deg,#3bb8ff,#8d76ff)}

.page-title{font-size:1.3rem;font-weight:900}.page-sub{color:var(--muted);font-size:.84rem;margin-top:3px}
.storage-pill{font-size:.72rem;color:var(--muted);padding:5px 8px;border:1px solid var(--line);border-radius:999px}

@media(max-width:900px){
  .kpi-grid,.forecast-grid,.bottom-grid{grid-template-columns:repeat(2,1fr)}
  .two-grid{grid-template-columns:1fr}
}
@media(max-width:620px){
  .kpi-grid,.forecast-grid,.bottom-grid{grid-template-columns:1fr}
  .fo-date{display:none}
}

.ai-hero {
    padding: 22px 24px;
    border-radius: 18px;
    border: 1px solid rgba(255,255,255,.09);
    background: linear-gradient(135deg, rgba(255,255,255,.055), rgba(255,255,255,.018));
    margin: 8px 0 16px 0;
}
.ai-kicker {font-size:.76rem; letter-spacing:.12em; opacity:.62; text-transform:uppercase;}
.ai-headline {font-size:1.55rem; font-weight:760; margin-top:5px;}
.ai-sub {opacity:.72; margin-top:5px; line-height:1.45;}
.action-card {
    min-height: 152px;
    padding: 18px;
    border-radius: 16px;
    border: 1px solid rgba(255,255,255,.08);
    background: rgba(255,255,255,.025);
}
.action-num {font-size:.72rem; opacity:.52; letter-spacing:.08em;}
.action-title {font-size:1.03rem; font-weight:720; margin:7px 0 4px;}
.action-tag {font-size:.78rem; opacity:.72; margin-bottom:8px;}
.action-why {font-size:.86rem; opacity:.67; line-height:1.4;}

:root {
    --v2-radius: 18px;
}
.block-container {
    max-width: 1180px;
    padding-top: 1.15rem;
    padding-bottom: 5rem;
}
.v2-hero {
    padding: 24px;
    border-radius: 22px;
    border: 1px solid rgba(255,255,255,.09);
    background: linear-gradient(145deg, rgba(255,255,255,.06), rgba(255,255,255,.018));
    margin: 6px 0 14px;
}
.v2-eyebrow {
    font-size: .72rem;
    letter-spacing: .13em;
    text-transform: uppercase;
    opacity: .56;
    margin-bottom: 6px;
}
.v2-big {
    font-size: clamp(1.65rem, 4vw, 2.55rem);
    line-height: 1.08;
    font-weight: 780;
}
.v2-copy {
    margin-top: 8px;
    opacity: .7;
    line-height: 1.45;
    max-width: 780px;
}
.v2-action {
    padding: 19px 21px;
    border: 1px solid rgba(255,255,255,.09);
    border-radius: 18px;
    background: rgba(255,255,255,.028);
    margin: 8px 0 14px;
}
.v2-action-title {font-size:1.18rem;font-weight:740;margin:4px 0;}
.v2-muted {opacity:.63;font-size:.88rem;line-height:1.45;}
.v2-section {margin-top:1.25rem;margin-bottom:.4rem;font-size:1.16rem;font-weight:730;}
.v2-status {
    display:inline-block;
    padding:3px 8px;
    border-radius:999px;
    border:1px solid rgba(255,255,255,.10);
    font-size:.72rem;
    opacity:.78;
}
div[data-testid="stMetric"] {
    border: 1px solid rgba(255,255,255,.07);
    border-radius: 16px;
    padding: 12px 14px;
    background: rgba(255,255,255,.018);
}
@media (max-width: 700px) {
    .block-container {padding-left: .85rem; padding-right: .85rem; padding-top:.65rem;}
    .v2-hero {padding:18px;}
    .v2-big {font-size:1.72rem;}
    div[data-testid="stHorizontalBlock"] {gap:.55rem;}
    div[data-testid="stMetric"] {padding:10px 11px;}
}

.v3-action-card{
    border:1px solid rgba(255,255,255,.12);
    border-radius:18px;
    padding:18px 20px;
    margin:14px 0 10px 0;
    background:rgba(255,255,255,.035);
}
</style>
""", unsafe_allow_html=True)


# ============================================================================
# FINANCE OS v3.1.1 — HOTFIX
# ============================================================================

def v3_money(value, decimals=0):
    """Currency text for v3 HTML/UI surfaces."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if decimals:
        return f"&#36;{amount:,.{int(decimals)}f}"
    return f"&#36;{amount:,.0f}"


# ============================================================================
# FINANCE OS v3.1 — AUDITABLE LEDGER + PRODUCT UI
# ============================================================================

def finance_today_v3():
    """One date for every v3 decision. Never mix workbook and system dates silently."""
    base = BASE.get("as_of")
    base_date = base.date() if isinstance(base, datetime) else base
    system_date = datetime.today().date()
    return max(base_date, system_date) if isinstance(base_date, date) else system_date


def protected_checking_target_v3():
    """
    Explicit user target only. v3 intentionally removes the hidden adaptive $1,500 floor.
    Falls back to the Finance OS protected-cash default.
    """
    saved = STATE.setdefault("settings", {}).get("protected_checking_target")
    if saved not in (None, ""):
        try:
            return max(0.0, float(saved))
        except (TypeError, ValueError):
            pass
    return max(0.0, float(BASE.get("protected_buffer", 1000) or 1000))


def discretionary_reserve_v3():
    """Optional explicit reserve for ordinary spending until payday."""
    try:
        return max(0.0, float(
            STATE.setdefault("settings", {}).get("discretionary_reserve_until_payday", 0) or 0
        ))
    except (TypeError, ValueError):
        return 0.0


def plaid_balance_source_v3(kind):
    """
    Current is the safe default. Plaid 'available' can be unusual for some institutions;
    v3 never swaps fields behind the user's back.
    """
    saved = STATE.setdefault("settings", {}).setdefault(
        "plaid_balance_source",
        {"checking": "current", "savings": "current"},
    )
    value = str(saved.get(kind, "current")).lower()
    return value if value in {"current", "available"} else "current"


def _mapped_plaid_account_v3(kind):
    mapping = STATE.get("plaid", {}).get("account_map", {}) or {}
    return plaid_account_by_id(mapping.get(kind))



def balance_resolution_v3(kind):
    cfg = STATE.setdefault("settings", {}).setdefault("balance_resolution", {})
    item = cfg.get(kind, {}) or {}
    mode = str(item.get("mode") or "").lower()
    if mode not in {"current", "available", "manual"}:
        mode = "unresolved"
    try:
        manual = float(item.get("manual")) if item.get("manual") not in (None, "") else None
    except (TypeError, ValueError):
        manual = None
    return {"mode": mode, "manual": manual, "verified_at": item.get("verified_at")}


def balance_is_unresolved_v3(kind):
    account = _mapped_plaid_account_v3(kind)
    if not account:
        return False
    conflict = plaid_balance_conflict_v3(kind)
    if not conflict:
        return False
    return balance_resolution_v3(kind)["mode"] == "unresolved"


def payday_detected_today_v3():
    today = finance_today_v3()
    try:
        rows = plaid_detected_paychecks()
    except Exception:
        rows = []
    matches = [x for x in rows if x.get("date") == today]
    if not matches:
        return None
    return {
        "date": today,
        "amount": sum(max(0.0, float(x.get("net", 0) or 0)) for x in matches),
        "description": " + ".join(sorted({str(x.get("description") or "Payroll") for x in matches})),
        "source": "Plaid verified payroll",
    }


def recent_payday_v3(days=3):
    today = finance_today_v3()
    try:
        rows = plaid_detected_paychecks()
    except Exception:
        rows = []
    matches = [
        x for x in rows
        if isinstance(x.get("date"), date) and 0 <= (today - x["date"]).days <= int(days)
    ]
    if not matches:
        return None
    latest_day = max(x["date"] for x in matches)
    same_day = [x for x in matches if x["date"] == latest_day]
    return {
        "date": latest_day,
        "amount": sum(max(0.0, float(x.get("net", 0) or 0)) for x in same_day),
        "description": " + ".join(sorted({str(x.get("description") or "Payroll") for x in same_day})),
        "source": "Plaid verified payroll",
    }



def authoritative_balance_field_v3(kind):
    """Return the actual source Finance OS is using after reconciliation."""
    resolution = balance_resolution_v3(kind)
    if resolution["mode"] in {"current", "available", "manual"}:
        return resolution["mode"]
    return plaid_balance_source_v3(kind)


def mapped_cash_balance_v3(kind):
    """
    v3.2.1 source-of-truth rule:
    - If the user explicitly verified Current, Available, or a manual balance,
      that verified choice is authoritative everywhere.
    - A mapped Plaid account with a completed sync can also supply the selected
      provisional field while reconciliation is unresolved.
    - The legacy `use_live_balances` toggle must NOT override a v3 reconciliation.
    """
    if kind not in {"checking", "savings"}:
        raise ValueError("kind must be checking or savings")

    account = _mapped_plaid_account_v3(kind)
    resolution = balance_resolution_v3(kind)

    # Explicit user verification wins over every legacy source toggle.
    if resolution["mode"] == "manual" and resolution["manual"] is not None:
        return float(resolution["manual"])

    if account and resolution["mode"] in {"current", "available"}:
        raw = account.get(resolution["mode"])
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass

    # While unresolved, use the configured provisional display field only if
    # Plaid has actually synced this account. Advice remains blocked elsewhere.
    if account and STATE.get("plaid", {}).get("last_sync"):
        source = plaid_balance_source_v3(kind)
        raw = account.get(source)
        fallback = "available" if source == "current" else "current"
        if raw is None:
            raw = account.get(fallback)
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            pass

    # No usable Plaid balance: fall back to the workbook/manual local ledger.
    if kind == "checking":
        amount = float(BASE.get("current_checking", 0) or 0)
        amount += float(STATE.get("settings", {}).get("checking_adjustment", 0) or 0)
    else:
        amount = float(BASE.get("current_savings", 0) or 0)
        amount += float(STATE.get("settings", {}).get("savings_adjustment", 0) or 0)

    for tx in STATE.get("transactions", []):
        try:
            d = tx_date(tx).date()
        except Exception:
            continue
        if d > finance_today_v3():
            continue
        val = float(tx.get("amount", 0) or 0)
        typ = tx.get("type")
        if kind == "checking":
            if typ == "Income":
                amount += val
            elif typ in {"Expense", "Card payment", "Transfer to savings"}:
                amount -= val
            elif typ == "Transfer from savings":
                amount += val
        else:
            if typ == "Transfer to savings":
                amount += val
            elif typ == "Transfer from savings":
                amount -= val
    return max(0.0, amount) if kind == "savings" else amount

def plaid_balance_conflict_v3(kind):
    account = _mapped_plaid_account_v3(kind)
    if not account:
        return None
    try:
        current = float(account.get("current")) if account.get("current") is not None else None
    except (TypeError, ValueError):
        current = None
    try:
        available = float(account.get("available")) if account.get("available") is not None else None
    except (TypeError, ValueError):
        available = None
    if current is None or available is None:
        return None
    diff = available - current
    if abs(diff) < max(100.0, abs(current) * .20):
        return None
    resolution = balance_resolution_v3(kind)
    selected = (
        resolution["mode"]
        if resolution["mode"] in {"current", "available", "manual"}
        else plaid_balance_source_v3(kind)
    )
    return {
        "current": current,
        "available": available,
        "difference": diff,
        "selected": selected,
    }


def _valid_month_day_v3(year, month, due_day):
    due_day = max(1, min(31, int(due_day)))
    while due_day >= 1:
        try:
            return date(year, month, due_day)
        except ValueError:
            due_day -= 1
    return date(year, month, 1)


def _month_iter_v3(start_date, end_date):
    y, m = start_date.year, start_date.month
    while date(y, m, 1) <= end_date:
        yield y, m
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


def _future_paychecks_v3(start_date, end_date):
    rows = []
    for row in rolling_forecast_base_rows(engine_count=40):
        d = row["date"].date()
        # Current checking already includes any paycheck that posted today.
        if d <= start_date or d > end_date:
            continue
        rows.append({
            "date": d,
            "name": "Paycheck",
            "kind": "paycheck",
            "amount": max(0.0, float(effective_paycheck(row))),
            "cash_impact": max(0.0, float(effective_paycheck(row))),
            "payment_method": "Checking",
            "source": "paycheck model",
            "confidence": "modeled",
        })
    return rows


def _future_household_income_v3(start_date, end_date):
    rows = []
    try:
        expected = expected_recurring_income_between(
            start_date + timedelta(days=1), end_date
        )
    except Exception:
        expected = []
    for x in expected:
        d = x.get("date")
        if not isinstance(d, date) or d <= start_date or d > end_date:
            continue
        amt = max(0.0, float(x.get("amount", 0) or 0))
        rows.append({
            "date": d,
            "name": str(x.get("source") or "Recurring household income"),
            "kind": "household_income",
            "amount": amt,
            "cash_impact": amt,
            "payment_method": "Checking",
            "source": f"recurring income • {x.get('status','Expected')}",
            "confidence": str(x.get("status", "expected")).lower(),
        })
    return rows


def _recurring_bill_events_v3(start_date, end_date):
    rows = []
    for bill in STATE.get("settings", {}).get("recurring_bills", []):
        if not bill.get("active"):
            continue
        for y, m in _month_iter_v3(start_date, end_date):
            d = _valid_month_day_v3(y, m, bill.get("due_day", 1))
            if d <= start_date or d > end_date:
                continue
            amount = max(0.0, float(bill.get("amount", 0) or 0))
            method = str(bill.get("payment_method") or "Checking")
            is_cash = method.strip().lower() in {"checking", "cash"}
            rows.append({
                "date": d,
                "name": str(bill.get("name") or "Bill"),
                "kind": "cash_bill" if is_cash else "card_charge",
                "amount": amount,
                "cash_impact": -amount if is_cash else 0.0,
                "payment_method": method,
                "source": "itemized recurring bill",
                "bill_id": bill.get("id"),
                "confidence": "known",
            })
    return rows



def _card_payment_match_terms_v3(card_name):
    """Conservative issuer/name terms used only to SUGGEST historical card payments."""
    name = str(card_name or "").upper()
    terms = {x for x in re.findall(r"[A-Z0-9]+", name) if len(x) >= 4}
    if "VENTURE" in name or "SAVOR" in name:
        terms.update({"CAPITAL ONE", "CAP ONE"})
    if "CREDIT ONE" in name:
        terms.update({"CREDIT ONE"})
    if "PLATINUM" in name:
        terms.update({"AMERICAN EXPRESS", "AMEX"})
    return sorted(terms, key=len, reverse=True)


def plaid_card_payment_suggestion_v3(card_name, lookback_days=180):
    """
    Suggest a payment amount/day from Plaid history; never auto-accept it.

    Requires a transaction classified as a credit-card payment plus issuer/name
    evidence in the bank description. Suggestions are intentionally conservative.
    """
    today = finance_today_v3()
    cutoff = today - timedelta(days=int(lookback_days))
    terms = _card_payment_match_terms_v3(card_name)
    hits = []

    for tx in STATE.get("plaid", {}).get("transactions", []) or []:
        if tx.get("pending"):
            continue
        try:
            d = datetime.fromisoformat(str(tx.get("date"))).date()
        except Exception:
            continue
        if d < cutoff or d > today:
            continue
        if classify_plaid_transaction(tx) != "credit_card_payment":
            continue

        amount = float(tx.get("amount", 0) or 0)
        if amount <= 0:
            continue

        desc = _tx_desc(tx).upper()
        if not any(term in desc for term in terms):
            continue

        hits.append({
            "date": d,
            "amount": amount,
            "description": tx.get("merchant_name") or tx.get("name") or "Card payment",
        })

    if not hits:
        return None

    hits.sort(key=lambda x: x["date"])
    recent = hits[-4:]
    amounts = [x["amount"] for x in recent]
    days = [x["date"].day for x in recent]
    typical = float(statistics.median(amounts))
    due_day = int(round(statistics.median(days)))

    # We are detecting historical payment behavior, NOT the contractual minimum.
    return {
        "amount": typical,
        "day": max(1, min(31, due_day)),
        "payments_found": len(hits),
        "recent_date": hits[-1]["date"],
        "recent_amount": hits[-1]["amount"],
        "description": hits[-1]["description"],
        "confidence": "medium" if len(hits) >= 2 else "low",
        "meaning": "historical payment pattern, not issuer minimum",
    }


def card_payment_rule_v3(card_name):
    rules = STATE.setdefault("settings", {}).setdefault("card_payment_rules", {})
    legacy = STATE.setdefault("settings", {}).setdefault("card_minimums", {}).get(card_name, {}) or {}
    raw = rules.get(card_name, {}) or {}

    mode = str(raw.get("mode") or ("minimum" if legacy.get("amount") else "unknown")).lower()
    if mode not in {"minimum", "fixed", "statement", "unknown"}:
        mode = "unknown"

    try:
        amount = max(0.0, float(raw.get("amount", legacy.get("amount", 0)) or 0))
    except (TypeError, ValueError):
        amount = 0.0

    actual_due_day = None
    for c in LIVE_CARDS:
        if str(c.get("card") or "") != str(card_name):
            continue
        raw_due = c.get("due")
        if isinstance(raw_due, datetime):
            actual_due_day = raw_due.day
        elif isinstance(raw_due, date):
            actual_due_day = raw_due.day
        elif isinstance(raw_due, (int, float)):
            try:
                actual_due_day = max(1, min(31, int(raw_due)))
            except Exception:
                actual_due_day = None
        break

    due_raw = raw.get("due_day", legacy.get("due_day"))
    try:
        due_day = int(due_raw) if due_raw not in (None, "") else None
    except (TypeError, ValueError):
        due_day = None

    if due_day is None:
        due_day = actual_due_day

    return {
        "mode": mode,
        "amount": amount,
        "due_day": (max(1, min(31, int(due_day))) if due_day is not None else None),
        "autopay": bool(raw.get("autopay", False)),
        "confirmed": bool(raw.get("confirmed", bool(legacy.get("amount")))),
    }

def card_payment_rules_summary_v3():
    rows = []
    for c in LIVE_CARDS:
        if float(c.get("balance", 0) or 0) <= .01:
            continue
        name = str(c.get("card") or "Card")
        rule = card_payment_rule_v3(name)
        suggestion = plaid_card_payment_suggestion_v3(name)
        rows.append({
            "card": name,
            "balance": float(c.get("balance", 0) or 0),
            "rule": rule,
            "suggestion": suggestion,
        })
    return rows


def _card_minimum_events_v3(start_date, end_date):
    """
    Model only user-confirmed card payment rules.

    minimum/fixed: repeat the explicit monthly amount.
    statement: use the current card balance only for the NEXT due date; later
               statement amounts remain unknown and are not invented.
    unknown: no cash event.
    """
    rows = []
    for card in LIVE_CARDS:
        name = str(card.get("card") or "Card")
        balance = max(0.0, float(card.get("balance", 0) or 0))
        if balance <= .01:
            continue

        rule = card_payment_rule_v3(name)
        if not rule["confirmed"] or rule["mode"] == "unknown":
            continue

        amount = rule["amount"]
        due_day = rule["due_day"]
        if due_day is None:
            continue

        due_dates = []
        for y, m in _month_iter_v3(start_date, end_date):
            d = _valid_month_day_v3(y, m, due_day)
            if start_date < d <= end_date:
                due_dates.append(d)

        if rule["mode"] == "statement":
            if due_dates:
                pay = min(balance, amount if amount > .005 else balance)
                if pay > .005:
                    rows.append({
                        "date": due_dates[0],
                        "name": f"{name} statement payment",
                        "kind": "card_payment",
                        "amount": pay,
                        "cash_impact": -pay,
                        "payment_method": "Checking",
                        "source": "confirmed statement-balance rule • next due only",
                        "confidence": "confirmed",
                    })
            continue

        if amount <= .005:
            continue

        label = "minimum" if rule["mode"] == "minimum" else "fixed"
        for d in due_dates:
            rows.append({
                "date": d,
                "name": f"{name} {label} payment",
                "kind": "card_payment",
                "amount": amount,
                "cash_impact": -amount,
                "payment_method": "Checking",
                "source": f"confirmed {label} card-payment rule",
                "confidence": "confirmed",
            })
    return rows

def _quick_entry_events_v3(start_date, end_date):
    rows = []
    for tx in STATE.get("transactions", []):
        try:
            d = tx_date(tx).date()
        except Exception:
            continue
        if d <= start_date or d > end_date:
            continue
        typ = str(tx.get("type") or "")
        amount = max(0.0, float(tx.get("amount", 0) or 0))
        name = str(tx.get("note") or tx.get("category") or typ or "Planned entry")
        impact = None
        kind = None

        if typ == "Income":
            impact, kind = amount, "planned_income"
        elif typ in {"Expense", "Card payment", "Transfer to savings"}:
            impact = -amount
            kind = {
                "Expense": "planned_expense",
                "Card payment": "card_payment",
                "Transfer to savings": "savings_transfer",
            }[typ]
        elif typ == "Transfer from savings":
            impact, kind = amount, "savings_transfer"

        if kind is None:
            continue
        rows.append({
            "date": d,
            "name": name,
            "kind": kind,
            "amount": amount,
            "cash_impact": impact,
            "payment_method": str(tx.get("card") or "Checking"),
            "source": "Quick Entry",
            "confidence": "explicit",
        })
    return rows


def _dedupe_ledger_events_v3(events):
    """
    Prevent exact duplicate obligations from being counted twice.
    Quick Entry wins over a recurring template if date + amount + normalized name match.
    """
    def key(e):
        clean = re.sub(r"[^A-Z0-9]", "", str(e.get("name","")).upper())
        return (e["date"], round(float(e.get("amount",0) or 0), 2), clean)

    ranked = sorted(
        events,
        key=lambda e: (
            e["date"],
            0 if e.get("source") == "Quick Entry" else 1,
            str(e.get("name","")),
        ),
    )
    out, seen = [], set()
    for e in ranked:
        k = key(e)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return sorted(out, key=lambda e: (e["date"], e.get("cash_impact",0) >= 0, str(e["name"])))



def forecast_completeness_v3():
    """
    A literal long-range checking forecast is only unlocked when each revolving
    card has a confirmed repeatable cash-payment rule.

    Statement-balance autopay is exact for the next known statement only; future
    statement amounts are unknown, so long-range checking remains provisional.
    """
    unresolved = []
    statement_cards = []
    for card in LIVE_CARDS:
        bal = max(0.0, float(card.get("balance", 0) or 0))
        if bal <= .01:
            continue
        name = str(card.get("card") or "Card")
        rule = card_payment_rule_v3(name)
        if not rule["confirmed"] or rule["mode"] == "unknown" or (
            rule["mode"] in {"minimum", "fixed"} and rule["amount"] <= .005
        ):
            unresolved.append(name)
        elif rule["mode"] == "statement":
            statement_cards.append(name)

    return {
        "long_range_ready": not unresolved and not statement_cards,
        "missing_card_minimums": unresolved,  # backwards-compatible key for existing UI
        "unresolved_cards": unresolved,
        "statement_cards": statement_cards,
    }

def ledger_commitment_path_v3(ledger):
    """
    Economic planning path, not literal bank checking:
    reserve every known card-funded charge until an explicit card cash payment is modeled.
    This prevents card spending from making future cash look artificially rich.
    """
    reserved = 0.0
    rows = [{
        "Date": ledger["start_date"],
        "Checking": float(ledger["starting_checking"]),
        "Committed cash": float(ledger["starting_checking"]),
        "Reserved card charges": 0.0,
    }]
    for e in ledger.get("events", []):
        if e.get("kind") == "card_charge":
            reserved += max(0.0, float(e.get("amount", 0) or 0))
        # explicit card payments already reduce checking; release no reserve here because
        # we cannot safely map a generic payment to a specific future recurring charge.
        rows.append({
            "Date": e["date"],
            "Checking": float(e.get("checking_after", 0) or 0),
            "Committed cash": float(e.get("checking_after", 0) or 0) - reserved,
            "Reserved card charges": reserved,
        })
    return rows


def cashflow_ledger_v3(days=120):
    """
    The new math engine.

    Starting actual checking
    + modeled/confirmed cash income
    - itemized cash obligations
    - explicitly entered card minimums/payments
    - explicit planned cash movements
    = projected checking.

    Card-funded purchases are visible events but do not reduce checking until a cash
    payment is actually modeled.
    """
    start = finance_today_v3()
    end = start + timedelta(days=max(1, int(days)))
    checking = mapped_cash_balance_v3("checking")

    events = []
    payday_today = payday_detected_today_v3()
    if payday_today:
        events.append({
            "date": payday_today["date"],
            "name": f"Paycheck received • {payday_today['description']}",
            "kind": "paycheck_received",
            "amount": payday_today["amount"],
            "cash_impact": 0.0,
            "payment_method": "Checking",
            "source": "Plaid verified • already reflected in synced starting balance",
            "confidence": "verified",
        })
    events += _future_paychecks_v3(start, end)
    events += _future_household_income_v3(start, end)
    events += _recurring_bill_events_v3(start, end)
    events += _card_minimum_events_v3(start, end)
    events += _quick_entry_events_v3(start, end)
    events = _dedupe_ledger_events_v3(events)

    running = float(checking)
    lowest = running
    for e in events:
        running += float(e.get("cash_impact", 0) or 0)
        e["checking_after"] = running
        lowest = min(lowest, running)

    return {
        "start_date": start,
        "end_date": end,
        "starting_checking": float(checking),
        "ending_checking": float(running),
        "lowest_checking": float(lowest),
        "events": events,
    }


def next_payday_v3():
    today = finance_today_v3()
    future = _future_paychecks_v3(today, today + timedelta(days=40))
    return future[0]["date"] if future else today + timedelta(days=14)


def plan_window_v3():
    today = finance_today_v3()
    payday = next_payday_v3()
    ledger = cashflow_ledger_v3(max(1, (payday - today).days))
    relevant = [e for e in ledger["events"] if e["date"] < payday]

    cash_in = sum(max(0.0, float(e["cash_impact"])) for e in relevant)
    cash_out = sum(max(0.0, -float(e["cash_impact"])) for e in relevant)
    card_charges = sum(
        float(e.get("amount",0) or 0)
        for e in relevant if e.get("kind") == "card_charge"
    )
    projected = ledger["starting_checking"] + cash_in - cash_out
    floor = protected_checking_target_v3()
    reserve = discretionary_reserve_v3()
    known_flexible = max(0.0, projected - floor)
    safe = max(0.0, known_flexible - reserve)

    return {
        "today": today,
        "next_payday": payday,
        "checking": ledger["starting_checking"],
        "savings": mapped_cash_balance_v3("savings"),
        "known_cash_in": cash_in,
        "known_cash_out": cash_out,
        "card_charges": card_charges,
        "projected_before_payday": projected,
        "protected_cash": floor,
        "discretionary_reserve": reserve,
        "known_flexible": known_flexible,
        "safe_to_spend": safe,
        "lowest_checking": ledger["lowest_checking"],
        "events": relevant,
        "checking_unresolved": balance_is_unresolved_v3("checking"),
        "savings_unresolved": balance_is_unresolved_v3("savings"),
        "recent_paycheck": recent_payday_v3(),
    }



def decision_window_unknowns_v3(plan=None):
    """
    Material unknowns that can make 'safe to spend' too strong before next payday.
    """
    plan = plan or plan_window_v3()
    today = plan["today"]
    payday = plan["next_payday"]
    unknowns = []

    # Any card due before payday without a confirmed payment rule blocks a true safe-to-spend label.
    for card in LIVE_CARDS:
        bal = max(0.0, float(card.get("balance", 0) or 0))
        if bal <= .01:
            continue
        name = str(card.get("card") or "Card")
        raw_due = card.get("due")
        due_date = None
        if isinstance(raw_due, datetime):
            due_date = raw_due.date()
        elif isinstance(raw_due, date):
            due_date = raw_due
        elif isinstance(raw_due, (int, float)):
            try:
                dday = int(raw_due)
                due_date = _valid_month_day_v3(today.year, today.month, dday)
                if due_date <= today:
                    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                    due_date = _valid_month_day_v3(y, m, dday)
            except Exception:
                due_date = None

        if due_date and today < due_date < payday:
            rule = card_payment_rule_v3(name)
            if not rule["confirmed"] or rule["mode"] == "unknown":
                unknowns.append({
                    "kind": "card_payment",
                    "name": name,
                    "due_date": due_date,
                    "reason": "required card payment rule is not confirmed",
                })

    return unknowns


def spendability_status_v3(plan=None):
    plan = plan or plan_window_v3()
    unknowns = decision_window_unknowns_v3(plan)

    if plan.get("checking_unresolved"):
        return {
            "label": "Balance verification required",
            "amount": None,
            "safe": False,
            "unknowns": unknowns,
            "detail": "Checking must be reconciled before Finance OS can evaluate discretionary spending.",
        }

    if unknowns:
        return {
            "label": "Flexible after known obligations",
            "amount": plan["safe_to_spend"],
            "safe": False,
            "unknowns": unknowns,
            "detail": (
                f"{len(unknowns)} required payment"
                + ("" if len(unknowns) == 1 else "s")
                + " before the next paycheck still needs confirmation."
            ),
        }

    return {
        "label": "Safe to spend",
        "amount": plan["safe_to_spend"],
        "safe": True,
        "unknowns": [],
        "detail": "Known required cash obligations in the decision window are modeled.",
    }


def paycheck_confidence_v3(event):
    return "verified" if str(event.get("confidence","")).lower() == "verified" else "modeled"


def data_health_v3():
    """Runtime self-audit. The app should surface uncertainty instead of hiding it."""
    issues = []
    today = finance_today_v3()

    # Balance-source metadata invariant.
    for kind in ("checking","savings"):
        resolution = balance_resolution_v3(kind)
        if resolution["mode"] in {"current","available","manual"}:
            actual_field = authoritative_balance_field_v3(kind)
            if actual_field != resolution["mode"]:
                issues.append({
                    "level":"critical",
                    "title":f"{kind.title()} source metadata mismatch",
                    "detail":f"Verified source is {resolution['mode']} but the engine reports {actual_field}."
                })

    # Plaid current/available conflicts.
    for kind in ("checking","savings"):
        c = plaid_balance_conflict_v3(kind)
        if c:
            resolution = balance_resolution_v3(kind)
            unresolved = resolution["mode"] == "unresolved"
            issues.append({
                "level":"critical" if unresolved and kind == "checking" else "info",
                "title": f"{kind.title()} balance needs verification" if unresolved else f"{kind.title()} balance fields disagree",
                "detail": (
                    f"Plaid current is {money(c['current'])}; available is {money(c['available'])}. "
                    + (
                        "Finance OS is blocking authoritative cash advice until you verify which balance is correct."
                        if unresolved else
                        f"You verified {resolution['mode']} as the balance Finance OS should use."
                    )
                ),
            })

    # Confirmed card-payment rules must have a real due day.
    for row in card_payment_rules_summary_v3():
        rule = row["rule"]
        if rule["confirmed"] and rule["mode"] != "unknown" and rule.get("due_day") is None:
            issues.append({
                "level":"critical",
                "title":f"{row['card']} payment rule has no due date",
                "detail":"A confirmed payment rule must have a real due day before Finance OS can schedule cash."
            })

    # Forecast completeness.
    completeness = forecast_completeness_v3()
    if not completeness["long_range_ready"]:
        unresolved = completeness.get("unresolved_cards", [])
        statement_cards = completeness.get("statement_cards", [])
        parts = []
        if unresolved:
            parts.append("payment rule needed for " + ", ".join(unresolved))
        if statement_cards:
            parts.append(
                "future statement amounts are naturally unknown for "
                + ", ".join(statement_cards)
            )
        issues.append({
            "level":"review",
            "title":"Long-range forecast needs card-payment rules",
            "detail":"; ".join(parts) + ". Near-term planning remains available without inventing future card payments.",
        })

    # Missing card limits. Card-payment gaps are grouped above instead of one warning per card.
    minimums = STATE.setdefault("settings", {}).setdefault("card_minimums", {})
    for card in LIVE_CARDS:
        bal = max(0.0, float(card.get("balance",0) or 0))
        if bal <= .01:
            continue
        if not card.get("limit_known"):
            issues.append({
                "level":"review",
                "title":f"{card.get('card','Card')} credit limit is missing",
                "detail":"Utilization and available credit cannot be considered reliable until the limit is known."
            })

    # Ledger invariant.
    ledger = cashflow_ledger_v3(90)
    running = ledger["starting_checking"]
    invariant_ok = True
    for e in ledger["events"]:
        running += float(e.get("cash_impact",0) or 0)
        if abs(running - float(e.get("checking_after",running))) > .011:
            invariant_ok = False
            break
    if not invariant_ok:
        issues.append({
            "level":"critical",
            "title":"Ledger arithmetic failed its self-check",
            "detail":"Projected checking does not reconcile event-by-event. Do not trust forecast numbers until corrected."
        })

    # Negative cash.
    if ledger["lowest_checking"] < 0:
        completeness = forecast_completeness_v3()
        issues.append({
            "level":"critical" if completeness["long_range_ready"] else "review",
            "title":"90-day modeled cash path falls below zero" if completeness["long_range_ready"] else "Long-range cash path needs review",
            "detail": (
                f"Modeled checking falls to {money(ledger['lowest_checking'])}. "
                + (
                    "Card cash-payment schedules are modeled; inspect the exact event in Plan."
                    if completeness["long_range_ready"] else
                    "Some card payment schedules are missing, so this is not treated as a final forecast."
                )
            )
        })

    # Duplicate-looking events after de-dupe detector.
    raw = (
        _future_paychecks_v3(today, today+timedelta(days=90))
        + _future_household_income_v3(today, today+timedelta(days=90))
        + _recurring_bill_events_v3(today, today+timedelta(days=90))
        + _card_minimum_events_v3(today, today+timedelta(days=90))
        + _quick_entry_events_v3(today, today+timedelta(days=90))
    )
    if len(raw) != len(_dedupe_ledger_events_v3(raw)):
        issues.append({
            "level":"info",
            "title":"Duplicate-looking planned events were de-duplicated",
            "detail":"Finance OS found the same date/name/amount more than once and counted it once. Review the Plan timeline if that was intentional."
        })

    # Infrastructure concern stays out of normal UI but is still discoverable.
    if plaid_settings() and not github_settings():
        issues.append({
            "level":"review",
            "title":"Persistent app state needs setup",
            "detail":"Plaid can sync now, but saved connection/reconciliation records are only local. Configure the private GitHub state secret before relying on redeploy persistence."
        })

    return issues


def confidence_label_v3():
    issues = data_health_v3()
    critical = sum(1 for x in issues if x["level"] == "critical")
    reviews = sum(1 for x in issues if x["level"] == "review")
    if critical:
        return "LOW", "Math/data issue needs attention"
    if reviews:
        return "REVIEW", f"{reviews} data checks need review"
    return "HIGH", "Known cash math reconciles"


def next_move_v3(plan=None):
    plan = plan or plan_window_v3()
    if plan.get("checking_unresolved"):
        return (
            "Verify checking first",
            "Plaid current and available disagree materially. Finance OS will not call you tight, safe, or overspent until the starting balance is reconciled."
        )
    if plan["projected_before_payday"] < 0:
        return "Protect cash", "Known obligations create a shortfall before the next paycheck."
    if plan["projected_before_payday"] < plan["protected_cash"]:
        gap = plan["protected_cash"] - plan["projected_before_payday"]
        return "Hold cash", f"You are {money(gap)} below your protected-checking target after known obligations."
    if plan["safe_to_spend"] > 0:
        return "You're in range", f"Known obligations and your protected cash target leave {money(plan['safe_to_spend'])} flexible."
    return "No extra move today", "Known obligations are covered, but there is no modeled flexible cash after your current reserve rules."


def paycheck_model_provenance_v3(pay_date):
    """Surface the historical seasonal basis behind a modeled paycheck."""
    if isinstance(pay_date, datetime):
        pay_date = pay_date.date()

    try:
        history = historical_pay_rows()
    except Exception:
        history = []

    candidates = []
    for row in history:
        d = row.get("date")
        if isinstance(d, datetime):
            d = d.date()
        if not isinstance(d, date) or d.year >= pay_date.year:
            continue
        try:
            net = float(row.get("net"))
        except Exception:
            continue

        # Circular calendar-day distance so late-Aug/early-Sep comparisons behave naturally.
        target_md = date(2000, pay_date.month, min(pay_date.day, 28))
        hist_md = date(2000, d.month, min(d.day, 28))
        delta = abs((hist_md - target_md).days)
        seasonal_distance = min(delta, 366 - delta)
        candidates.append({
            "date": d,
            "net": net,
            "seasonal_distance_days": seasonal_distance,
            "sales_period": sales_period_label(d),
        })

    candidates.sort(key=lambda x: (x["seasonal_distance_days"], -x["date"].year))
    analogs = candidates[:2]
    return {
        "method": "historical seasonal commission model",
        "confidence": "modeled",
        "analogs": [
            {
                "date": x["date"].isoformat(),
                "net": round(x["net"], 2),
                "seasonal_distance_days": int(x["seasonal_distance_days"]),
                "sales_period": x["sales_period"],
            } for x in analogs
        ],
        "analog_average": (
            round(sum(x["net"] for x in analogs) / len(analogs), 2)
            if analogs else None
        ),
    }


def ai_ledger_events_v3(ledger_events, next_payday, long_range_ready):
    """
    Keep event facts, but withhold precise cumulative checking after the trusted
    near-term window when long-range cash completeness is not established.
    """
    rows = []
    for e in ledger_events:
        row = dict(e)
        d = row.get("date")
        if isinstance(d, datetime):
            d = d.date()
        if not long_range_ready and isinstance(d, date) and d > next_payday:
            row["checking_after"] = None
            row["checking_after_status"] = "withheld_incomplete_forecast"
        rows.append(row)
    return rows



def unresolved_required_cash_events_v3(days=120):
    """Required cash obligations that currently block an exact cumulative checking forecast."""
    today = finance_today_v3()
    horizon = today + timedelta(days=days)
    unknowns = []

    for card in LIVE_CARDS:
        balance = max(0.0, float(card.get("balance", 0) or 0))
        if balance <= .01:
            continue

        name = str(card.get("card") or "Card")
        rule = card_payment_rule_v3(name)
        raw_due = card.get("due")
        due_date = None

        if isinstance(raw_due, datetime):
            due_date = raw_due.date()
        elif isinstance(raw_due, date):
            due_date = raw_due
        elif isinstance(raw_due, (int, float)):
            try:
                due_day = int(raw_due)
                due_date = _valid_month_day_v3(today.year, today.month, due_day)
                if due_date < today:
                    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                    due_date = _valid_month_day_v3(y, m, due_day)
            except Exception:
                due_date = None

        if due_date and today <= due_date <= horizon:
            if not rule["confirmed"] or rule["mode"] == "unknown":
                unknowns.append({
                    "date": due_date,
                    "kind": "card_payment",
                    "name": name,
                    "reason": "card payment amount/mode is not confirmed",
                })
            elif rule["mode"] == "statement" and float(rule.get("amount", 0) or 0) <= 0:
                unknowns.append({
                    "date": due_date,
                    "kind": "card_payment",
                    "name": name,
                    "reason": "upcoming statement payment amount is not supplied",
                })

    unknowns.sort(key=lambda x: (x["date"], x["name"]))
    return unknowns


def trust_horizon_v3(days=120):
    today = finance_today_v3()
    unknowns = unresolved_required_cash_events_v3(days)
    if not unknowns:
        trusted = today + timedelta(days=days)
        return {
            "status": "clear",
            "trusted_through": trusted,
            "first_blocker": None,
            "unknowns": [],
            "label": f"Trusted through {trusted.strftime('%b %d')}",
        }

    blocker = unknowns[0]
    trusted = max(today, blocker["date"] - timedelta(days=1))
    return {
        "status": "blocked",
        "trusted_through": trusted,
        "first_blocker": blocker,
        "unknowns": unknowns,
        "label": f"Trusted through {trusted.strftime('%b %d')}",
    }


def planning_confidence_v3():
    plan = plan_window_v3()
    trust = trust_horizon_v3(120)
    complete = forecast_completeness_v3()

    if plan.get("checking_unresolved"):
        today_conf = "Needs verification"
        next_conf = "Needs verification"
    else:
        today_conf = "High"
        next_conf = "High" if trust["trusted_through"] >= plan["next_payday"] else "Provisional"

    return {
        "today": today_conf,
        "through_next_paycheck": next_conf,
        "days_30_90": "High" if complete["long_range_ready"] else "Provisional",
        "trust_horizon": trust,
    }


def redact_after_trust_horizon_v3(events, trusted_through):
    rows = []
    for e in events:
        row = dict(e)
        d = row.get("date")
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date) and d > trusted_through:
            row["checking_after"] = None
            row["checking_after_status"] = "withheld_beyond_trust_horizon"
        rows.append(row)
    return rows


def card_setup_readiness_v3():
    rows = card_payment_rules_summary_v3()
    complete, incomplete = [], []
    for row in rows:
        rule = row["rule"]
        ready = bool(
            rule["confirmed"]
            and rule["mode"] != "unknown"
            and rule.get("due_day") is not None
            and (rule["mode"] == "statement" or float(rule.get("amount", 0) or 0) > 0)
        )
        (complete if ready else incomplete).append(row)

    return {
        "total": len(rows),
        "complete": len(complete),
        "incomplete": len(incomplete),
        "complete_rows": complete,
        "incomplete_rows": incomplete,
    }


def persistence_readiness_v3():
    return {
        "github_state": bool(github_settings()),
        "token_encryption": bool(plaid_crypto_ready()),
        "ready_for_critical_setup": bool(github_settings()),
    }


def _ai_snapshot_v3():
    plan = plan_window_v3()
    ledger = cashflow_ledger_v3(90)
    issues = data_health_v3()
    completeness = forecast_completeness_v3()
    spendability = spendability_status_v3(plan)
    confidence = planning_confidence_v3()
    trust = confidence["trust_horizon"]

    ai_events = redact_after_trust_horizon_v3(
        ledger["events"][:70],
        trust["trusted_through"],
    )

    event_rows = []
    for e in ai_events:
        checking_after = e.get("checking_after")
        event_rows.append({
            "date": e["date"].isoformat(),
            "name": e["name"],
            "type": e["kind"],
            "amount": round(float(e["amount"]),2),
            "cash_impact": round(float(e["cash_impact"]),2),
            "checking_after": (
                round(float(checking_after),2)
                if checking_after is not None else None
            ),
            "checking_after_status": e.get("checking_after_status"),
            "payment_method": e.get("payment_method"),
            "source": e.get("source"),
            "confidence": ("modeled" if e.get("kind") == "paycheck" else e.get("confidence")),
            "paycheck_model_basis": (
                paycheck_model_provenance_v3(e["date"])
                if e.get("kind") == "paycheck" else None
            ),
        })

    cards = []
    minimums = STATE.setdefault("settings", {}).setdefault("card_minimums", {})
    for c in LIVE_CARDS:
        cards.append({
            "name": c.get("card"),
            "balance": round(float(c.get("balance",0) or 0),2),
            "limit": round(float(c.get("limit",0) or 0),2) if c.get("limit_known") else None,
            "utilization_pct": round(float(c.get("util",0) or 0)*100,1) if c.get("limit_known") else None,
            "minimum_payment": float((minimums.get(c.get("card"), {}) or {}).get("amount",0) or 0) or None,
            "due": str(c.get("due") or "") or None,
        })

    checking_resolution = balance_resolution_v3("checking")
    savings_resolution = balance_resolution_v3("savings")

    return {
        "as_of": finance_today_v3().isoformat(),
        "engine": "Finance OS v3 itemized cash ledger",
        "checking_balance_used": round(plan["checking"],2),
        "checking_balance_field": authoritative_balance_field_v3("checking"),
        "checking_balance_choice_status": (
            "user_verified" if checking_resolution["mode"] in {"current","available","manual"} else "unresolved"
        ),
        "savings_balance_used": round(plan["savings"],2),
        "savings_balance_field": authoritative_balance_field_v3("savings"),
        "savings_balance_choice_status": (
            "user_verified" if savings_resolution["mode"] in {"current","available","manual"} else "unresolved"
        ),
        "next_payday": plan["next_payday"].isoformat(),
        "known_cash_in_before_payday": round(plan["known_cash_in"],2),
        "known_cash_out_before_payday": round(plan["known_cash_out"],2),
        "card_funded_charges_before_payday": round(plan["card_charges"],2),
        "protected_checking_target": round(plan["protected_cash"],2),
        "explicit_discretionary_reserve": round(plan["discretionary_reserve"],2),
        "projected_checking_before_payday": round(plan["projected_before_payday"],2),
        "safe_to_spend": (round(plan["safe_to_spend"],2) if spendability["safe"] else None),
        "flexible_after_known_obligations": (
            round(plan["safe_to_spend"],2) if not spendability["safe"] and not plan.get("checking_unresolved") else None
        ),
        "spendability_status": spendability["label"],
        "spendability_unknowns": spendability["unknowns"],
        "planning_confidence": {
            "today": confidence["today"],
            "through_next_paycheck": confidence["through_next_paycheck"],
            "days_30_90": confidence["days_30_90"],
        },
        "compact_audit_status": compact_audit_context_v3(),
        "trust_horizon": {
            "trusted_through": trust["trusted_through"].isoformat(),
            "status": trust["status"],
            "first_blocker": (
                {
                    "date": trust["first_blocker"]["date"].isoformat(),
                    "name": trust["first_blocker"]["name"],
                    "kind": trust["first_blocker"]["kind"],
                    "reason": trust["first_blocker"]["reason"],
                }
                if trust["first_blocker"] else None
            ),
        },
        "long_range_forecast_status": "ready" if completeness["long_range_ready"] else "incomplete",
        "long_range_forecast_reason": {
            "unresolved_cards": completeness.get("unresolved_cards", []),
            "statement_cards": completeness.get("statement_cards", []),
        },
        "lowest_known_checking_90d": (
            round(ledger["lowest_checking"],2) if completeness["long_range_ready"] else None
        ),
        "data_health": issues,
        "cards": cards,
        "next_90_day_ledger": event_rows,
        "checking_balance_unresolved": bool(plan.get("checking_unresolved")),
        "savings_balance_unresolved": bool(plan.get("savings_unresolved")),
        "recent_verified_paycheck": (
            {
                "date": plan["recent_paycheck"]["date"].isoformat(),
                "amount": round(float(plan["recent_paycheck"]["amount"]), 2),
                "description": plan["recent_paycheck"]["description"],
            }
            if plan.get("recent_paycheck") else None
        ),
        "card_payment_rules": [
            {
                "card": x["card"],
                "mode": x["rule"]["mode"],
                "amount": round(float(x["rule"]["amount"]),2),
                "due_day": x["rule"]["due_day"],
                "autopay": x["rule"]["autopay"],
                "confirmed": x["rule"]["confirmed"],
            }
            for x in card_payment_rules_summary_v3()
        ],
        "definitions": {
            "safe_to_spend": (
                "Only populated when all required payments before next payday are sufficiently known. "
                "Otherwise use flexible_after_known_obligations, which is not a discretionary budget."
            ),
            "trust_horizon": (
                "checking_after is intentionally null after trusted_through. Future events remain visible, "
                "but Finance OS does not claim an exact cumulative checking balance beyond the first unresolved required cash event."
            ),
        },
    }

def call_finance_ai_v3(question):
    """
    Copilot is explanation + audit + scenario reasoning.
    It cannot overwrite deterministic ledger math.
    """
    cfg = openai_settings()
    if not cfg:
        raise RuntimeError("OpenAI API is not configured in Streamlit Secrets.")

    snapshot = _ai_snapshot_v3()
    system = """
You are Finance OS Copilot inside a private personal-finance application.

The v3 deterministic itemized cash ledger is the arithmetic source of truth for known events.
Your job is more powerful than summarization: audit the plan, challenge assumptions, explain tradeoffs,
spot suspicious data, and reason through scenarios. Never invent missing facts.

Rules:
- Never invent a balance, bill, paycheck, APR, card minimum, due date, fee, payoff date, or transaction.
- A Plaid-derived card-payment suggestion is historical behavior only. Treat a card payment rule as authoritative only when its confirmed field is true.
- Statement-balance rules may define the next cash payment, but future statement amounts are unknown unless explicitly supplied; never extrapolate them as exact.
- If a required fact is missing, say what is missing and how it limits the recommendation.
- Treat data-health warnings as important. If current and available balances disagree, do not choose one yourself.
- If checking_balance_unresolved is true, do not declare a cash emergency, affordability result, safe-to-spend result, or debt-payment recommendation. Lead with "Verify checking balance first."
- If checking_balance_choice_status is "user_verified", accept that selected Plaid field as the planning source. You may mention the field difference once as informational, but do not frame the verified choice itself as suspicious or tell the user to re-reconcile it without new evidence.
- recent_verified_paycheck is authoritative evidence that payroll was detected by Plaid. A zero-impact paycheck_received event means the deposit is already reflected in the synced starting balance and must not be added again.
- Be exact with dates. Never call an event "tomorrow", "today", "this weekend", or similar unless that wording is mathematically correct relative to snapshot as_of. Prefer the explicit date when there is any doubt.
- Do not tell the user to carry interest-bearing debt merely to preserve utilization optics.
- Prioritize: required obligations -> avoid negative cash -> explicit protected checking -> unnecessary interest ->
  dangerous utilization -> savings/optimization.
- Distinguish "technically affordable" from "wise to buy now."
- If spendability_status is not "Safe to spend", do not call the flexible amount safe or discretionary. Use the exact supplied label.
- If long_range_forecast_status is "incomplete", do not quote or infer an exact future checking balance from the 90-day ledger. Explain that modeled payroll and missing card-payment cash flows make long-range cash provisional.
- Respect trust_horizon.trusted_through as the final date with exact cumulative checking math. Never reconstruct a checking balance after that date from later events.
- For audit requests, lead with compact_audit_status. If the same blocker is already known and unchanged, do not spend most of the response re-explaining it. Prioritize newly discovered defects, changed risks, or the single next action.
- Treat modeled paychecks as forecasts, not known cash. Explicitly use words like "modeled", "estimated", or "expected" when relying on them.
- When paycheck_model_basis is supplied, explain its historical analog basis before calling paycheck variation unexplained. You may challenge confidence, but do not describe a documented seasonal model as having no basis.
- If Finance OS's recommendation does not pass a common-sense test, say so and identify the exact assumption.
- Lead with a direct answer, then a short rationale and concrete next action.
- You are not allowed to mutate balances, state, bank connections, or payments.
- Never expose or request Plaid tokens, bank credentials, API keys, or account numbers.
""".strip()

    user_content = (
        "Finance OS sanitized snapshot:\n"
        + json.dumps(snapshot, indent=2, default=str)
        + "\n\nUser request:\n"
        + str(question)
    )

    def run_request(reasoning_effort, max_tokens):
        payload = {
            "model": cfg["model"],
            "reasoning": {"effort": reasoning_effort},
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_output_tokens": int(max_tokens),
        }

        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
                "User-Agent": "Finance-OS-Streamlit/3.4.1",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=75) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            try:
                message = json.loads(body).get("error", {}).get("message") or body
            except Exception:
                message = body
            raise RuntimeError(f"OpenAI API error: {message}") from exc
        except Exception as exc:
            raise RuntimeError(f"OpenAI request failed: {exc}") from exc

    # Audit prompts can consume significant reasoning tokens before producing visible text.
    # Give them enough headroom on the first request.
    result = run_request(cfg.get("reasoning_effort", "medium"), 3200)
    text = _extract_openai_response_text(result)
    if text:
        return text

    # Successful response but no user-visible text: retry once with low reasoning so the
    # token budget is overwhelmingly available for a visible answer.
    first_reason = _openai_no_text_reason(result)
    retry = run_request("low", 2400)
    text = _extract_openai_response_text(retry)
    if text:
        return text

    retry_reason = _openai_no_text_reason(retry)
    raise RuntimeError(
        "OpenAI completed the request but Finance OS still received no visible answer. "
        f"First attempt: {first_reason} Retry: {retry_reason}"
    )

def cached_copilot_v3(question):
    snapshot_key = hashlib.sha256(
        json.dumps(_ai_snapshot_v3(), sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    cache = st.session_state.setdefault("v3_ai_cache", {})
    key = hashlib.sha256((snapshot_key + "|" + question).encode("utf-8")).hexdigest()
    if key in cache:
        return cache[key], True
    answer = call_finance_ai_v3(question)
    cache[key] = answer
    # tiny session cache
    while len(cache) > 10:
        cache.pop(next(iter(cache)))
    return answer, False


def spending_story_v3():
    intel = spending_intelligence()
    if not intel:
        return None
    cats = intel.get("categories", []) or []
    changes = sorted(
        cats,
        key=lambda x: abs(float(x.get("Change / mo",0) or 0)),
        reverse=True,
    )
    return {
        "recent": float(intel.get("recent_monthly",0) or 0),
        "prior": float(intel.get("prior_monthly",0) or 0),
        "change": float(intel.get("recent_monthly",0) or 0) - float(intel.get("prior_monthly",0) or 0),
        "changes": changes[:8],
    }


def plaid_autosync_once_v3():
    """
    Attempt one Plaid refresh per new Streamlit browser session.

    Streamlit reruns the script on every interaction, so the session-state guard is
    essential: this is "once when you open/login", not "once per rerun".
    Failure is non-fatal and never creates a new Plaid Item.
    """
    key = "v331_plaid_autosync_attempted"
    if st.session_state.get(key):
        return st.session_state.get("v331_plaid_autosync_result")

    st.session_state[key] = True
    result = {
        "attempted": False,
        "success": False,
        "message": None,
        "at": datetime.now().isoformat(timespec="seconds"),
    }

    plaid = STATE.get("plaid", {}) or {}
    if not plaid_settings():
        result["message"] = "Plaid is not configured."
        st.session_state["v331_plaid_autosync_result"] = result
        return result

    if not (plaid.get("items") or []):
        result["message"] = "No saved Plaid connections are available."
        st.session_state["v331_plaid_autosync_result"] = result
        return result

    result["attempted"] = True
    try:
        refreshed = refresh_all_plaid_v3()
        # Persist refreshed cursors/balances when storage is configured; local fallback
        # remains functional for the current app instance.
        try:
            save_state(STATE, message="Finance OS v3.3.1: automatic Plaid refresh")
        except Exception as save_exc:
            result["save_warning"] = str(save_exc)

        result.update({
            "success": True,
            "message": (
                f"Auto-refreshed {refreshed['connections_refreshed']} Plaid connection"
                f"{'' if refreshed['connections_refreshed'] == 1 else 's'}."
            ),
            "details": refreshed,
        })
    except Exception as exc:
        result["message"] = f"Automatic bank refresh could not complete: {exc}"

    st.session_state["v331_plaid_autosync_result"] = result
    return result


PLAID_AUTOSYNC_V3 = plaid_autosync_once_v3()


# --- v3 visual language -------------------------------------------------------
st.markdown("""
<style>
:root{
  --fos-card: rgba(255,255,255,.045);
  --fos-border: rgba(255,255,255,.085);
  --fos-soft: rgba(255,255,255,.62);
}
.block-container{max-width:1050px;padding-top:.75rem;padding-bottom:5rem;}
header[data-testid="stHeader"]{background:transparent;}
.v3-brand{display:flex;align-items:center;justify-content:space-between;margin:3px 0 14px;}
.v3-logo{font-size:1.05rem;font-weight:780;letter-spacing:-.02em;}
.v3-version{font-size:.72rem;opacity:.42;}
.v3-hero{
  border:1px solid var(--fos-border);
  border-radius:22px;
  padding:22px;
  background:
    radial-gradient(circle at 85% 10%, rgba(92,122,255,.17), transparent 32%),
    linear-gradient(145deg, rgba(255,255,255,.055), rgba(255,255,255,.018));
  margin-bottom:14px;
}
.v3-kicker{text-transform:uppercase;letter-spacing:.13em;font-size:.68rem;opacity:.5;margin-bottom:7px;}
.v3-number{font-size:clamp(2.35rem,6vw,4.35rem);font-weight:800;line-height:.98;letter-spacing:-.045em;}
.v3-label{font-size:1.05rem;font-weight:680;margin-top:8px;}
.v3-sub{max-width:720px;opacity:.62;margin-top:7px;line-height:1.45;}
.v3-section{font-size:1.08rem;font-weight:760;margin:1.45rem 0 .55rem;}
.v3-card{
  border:1px solid var(--fos-border);border-radius:19px;padding:17px 18px;
  background:var(--fos-card);height:100%;
}
.v3-card-eyebrow{font-size:.7rem;letter-spacing:.1em;text-transform:uppercase;opacity:.48;}
.v3-card-value{font-size:1.38rem;font-weight:760;margin-top:5px;letter-spacing:-.025em;}
.v3-card-copy{font-size:.83rem;opacity:.58;margin-top:5px;line-height:1.35;}
.v3-move{
  border:1px solid rgba(123,147,255,.22);border-radius:20px;padding:19px 20px;
  background:linear-gradient(135deg,rgba(94,119,255,.12),rgba(255,255,255,.025));
}
.v3-move-title{font-size:1.25rem;font-weight:780;}
.v3-move-copy{opacity:.63;margin-top:5px;}
.v3-health{display:inline-block;border:1px solid var(--fos-border);border-radius:999px;padding:4px 9px;font-size:.72rem;opacity:.68;}
.v3-attention{border:1px solid var(--fos-border);border-radius:16px;padding:13px 15px;margin:7px 0;background:rgba(255,255,255,.025);}
.v3-attention-title{font-weight:700;}
.v3-attention-copy{font-size:.82rem;opacity:.6;margin-top:3px;}
div[data-testid="stMetric"]{border:1px solid var(--fos-border);border-radius:17px;padding:12px 14px;background:rgba(255,255,255,.025);}
div[data-testid="stDataFrame"]{border-radius:16px;overflow:hidden;}
.stButton>button{border-radius:13px;}
@media(max-width:700px){
  .block-container{padding-left:.72rem;padding-right:.72rem;padding-top:.35rem;}
  .v3-hero{padding:20px;border-radius:22px;}
  .v3-number{font-size:3.3rem;}
  .v3-card{padding:14px;}
}
</style>
""", unsafe_allow_html=True)



# ---------- Finance OS 4.0 journey layer ----------

def v4_goals():
    return [g for g in STATE.setdefault('goals', []) if isinstance(g, dict) and g.get('name')]

def v4_goal_current(goal):
    kind = str(goal.get('kind', 'savings')).lower()
    if kind in {'savings','emergency'}:
        return max(0.0, float(goal.get('current_override', mapped_cash_balance_v3('savings')) or 0))
    if kind == 'purchase':
        return max(0.0, float(goal.get('saved', goal.get('current_override', 0)) or 0))
    if kind == 'debt':
        card = str(goal.get('card','')).strip().lower()
        for c in LIVE_CARDS:
            if str(c.get('card','')).strip().lower() == card:
                return max(0.0, float(c.get('balance',0) or 0))
        return max(0.0, float(goal.get('current_override', goal.get('start_amount',0)) or 0))
    return max(0.0, float(goal.get('current_override',0) or 0))

def v4_goal_status(goal):
    today = finance_today_v3(); kind = str(goal.get('kind','savings')).lower()
    current = v4_goal_current(goal); target = max(0.0, float(goal.get('target',0) or 0)); start_amount = float(goal.get('start_amount', current) or current)
    try: start = date.fromisoformat(str(goal.get('start_date') or today.isoformat())[:10])
    except Exception: start = today
    try: deadline = date.fromisoformat(str(goal.get('deadline'))[:10]) if goal.get('deadline') else None
    except Exception: deadline = None
    if kind == 'debt':
        base=max(.01,start_amount); progress=min(1.0,max(0.0,(base-current)/base)); remaining=current
    else:
        span=max(.01,target-start_amount); progress=min(1.0,max(0.0,(current-start_amount)/span)) if target>start_amount else 1.0; remaining=max(0.0,target-current)
    ahead=None; projected=None; required=None
    if deadline and deadline>start:
        elapsed=max(0,(today-start).days); total=max(1,(deadline-start).days); ratio=min(1.0,elapsed/total)
        expected = max(0.0,start_amount*(1-ratio)) if kind=='debt' else start_amount+(target-start_amount)*ratio
        ahead = expected-current if kind=='debt' else current-expected
        checks=max(1,round(max(1,(deadline-today).days)/14)); required=remaining/checks
        if progress>0 and elapsed>0:
            projected=start+timedelta(days=round(1/(progress/elapsed)))
    return {'current':current,'target':target,'remaining':remaining,'progress':progress,'ahead_amount':ahead,'deadline':deadline,'projected_date':projected,'required_per_paycheck':required}

def v4_financial_score(plan=None):
    plan=plan or plan_window_v3(); spend=spendability_status_v3(plan); health=data_health_v3()
    total_bal=sum(max(0,float(c.get('balance',0) or 0)) for c in LIVE_CARDS); total_lim=sum(max(0,float(c.get('limit',0) or 0)) for c in LIVE_CARDS if c.get('limit')); util=total_bal/total_lim if total_lim else 0
    gs=[v4_goal_status(g) for g in v4_goals()]; on=sum(1 for s in gs if s.get('ahead_amount') is None or s.get('ahead_amount',0)>=-25)
    parts={'Bills':10 if plan.get('projected_before_payday',0)>=0 else 2,'Cash safety':10 if plan.get('projected_before_payday',0)>=plan.get('protected_cash',0) else 4,'Spending':8 if spend.get('safe') else 5 if (spend.get('amount') or 0)>0 else 3,'Debt':9 if util<.1 else 7 if util<.3 else 5 if util<.5 else 3,'Income':7 if _future_paychecks_v3(finance_today_v3(),finance_today_v3()+timedelta(days=30)) else 3,'Goal progress':10 if not gs else round(10*on/len(gs))}
    base=round((parts['Bills']*2+parts['Cash safety']*2+parts['Spending']+parts['Debt']+parts['Income']+parts['Goal progress']*2)/9*10)
    penalty=sum(12 if x.get('level')=='critical' else 2 if x.get('level')=='review' else 0 for x in health)
    return int(max(0,min(100,base-penalty))),parts

def v4_mode(plan):
    if plan.get('projected_before_payday',0)<plan.get('protected_cash',0): return 'RECOVERY'
    if plan.get('safe_to_spend',0)>=max(500,plan.get('protected_cash',0)*.5): return 'OPPORTUNITY'
    return 'STEADY'

def v4_recommendation(plan):
    spend=spendability_status_v3(plan); mode=v4_mode(plan)
    if mode=='RECOVERY':
        gap=max(0,plan['protected_cash']-plan['projected_before_payday']); return 'Protect cash until payday',f"You're about {money(gap)} below your protected-cash target after known obligations. Pause extra debt payments and optional savings until {plan['next_payday'].strftime('%b %d')}."
    if not spend.get('safe') and spend.get('unknowns'):
        name=spend['unknowns'][0].get('name','required payment'); return f'Confirm {name}', 'Once this required payment is confirmed, Finance OS can give you a true Safe to Spend number.'
    if mode=='OPPORTUNITY': return 'Use the surplus intentionally',f"You have {money(plan.get('safe_to_spend',0))} above known obligations and protected cash. Send the next extra dollar toward your highest-priority goal, then keep some guilt-free spending money."
    return 'Stay the course',f"Known obligations are covered through {plan['next_payday'].strftime('%b %d')}. Keep protected cash intact and follow the next goal contribution."

def v4_add_goal(name,kind,target,deadline,priority,current=None,card=None):
    today=finance_today_v3(); start=current if current is not None else (mapped_cash_balance_v3('savings') if kind in {'savings','emergency'} else 0)
    STATE.setdefault('goals',[]).append({'id':hashlib.sha1(f'{name}-{datetime.now().isoformat()}'.encode()).hexdigest()[:10],'name':name.strip(),'kind':kind,'target':float(target),'deadline':deadline.isoformat(),'priority':priority,'start_date':today.isoformat(),'start_amount':float(start or 0),'current_override':float(current) if current is not None and kind!='debt' else None,'card':card})
    save_and_reload('Finance OS 4.1: add journey goal')

st.markdown('''<style>
.block-container{max-width:860px;padding-top:1.2rem;padding-bottom:5rem}.v4-hero{padding:24px;border:1px solid #2b3a4c;border-radius:26px;background:linear-gradient(145deg,#111c29,#172748);margin:12px 0 18px}.v4-kicker{font-size:.76rem;letter-spacing:.15em;text-transform:uppercase;color:#96a0ad;margin-bottom:7px}.v4-big{font-size:3.5rem;font-weight:850;line-height:1;color:#fff}.v4-sub{font-size:1.08rem;font-weight:700;color:#fff;margin-top:8px}.v4-muted{color:#9ba6b2;margin-top:7px}.v4-card{padding:18px;border:1px solid #263547;border-radius:20px;background:#0d1723;margin:9px 0}.v4-row{display:flex;justify-content:space-between;gap:14px;align-items:center}.v4-title{font-size:1.08rem;font-weight:800}.v4-score{font-size:2rem;font-weight:850}.v4-progress{height:10px;background:#1e2936;border-radius:999px;overflow:hidden;margin:12px 0}.v4-progress span{display:block;height:100%;background:#5b8cff;border-radius:999px}.v4-section{font-size:1.35rem;font-weight:850;margin:26px 0 9px}.v4-good{color:#62dc9a}.v4-warn{color:#f7c95c}.v4-pill{display:inline-block;border:1px solid #33455a;border-radius:999px;padding:4px 9px;color:#c2cad4;font-size:.78rem}@media(max-width:640px){.block-container{padding-left:1rem;padding-right:1rem}.v4-big{font-size:3.1rem}}</style>''',unsafe_allow_html=True)

try: plaid_autosync_once_v3()
except Exception: pass
plan=plan_window_v3(); spend=spendability_status_v3(plan); score,score_parts=v4_financial_score(plan); mode=v4_mode(plan); rec_title,rec_detail=v4_recommendation(plan)
st.markdown(f"### Finance OS <span style='float:right;color:#6f7a88;font-size:.8rem'>{APP_VERSION}</span>",unsafe_allow_html=True)
page=st.radio('', ['Today','Journey','Plan','Money','More'],horizontal=True,key='v4_nav',label_visibility='collapsed')

if page=='Today':
    amt=spend.get('amount'); amount_text=money(amt) if amt is not None else '—'; qualifier='SAFE TO SPEND' if spend.get('safe') else 'AVAILABLE AFTER KNOWN BILLS'; tone='Comfortable' if spend.get('safe') and (amt or 0)>250 else 'Tight' if (amt or 0)<150 else 'Watch'
    st.markdown(f"<div class='v4-hero'><div class='v4-kicker'>Today · {mode.title()} mode</div><div class='v4-big'>{amount_text}</div><div class='v4-sub'>{qualifier} until {plan['next_payday'].strftime('%b %d')}</div><div class='v4-muted'>Bills modeled · {money(plan['protected_cash'])} protected · {tone}</div></div>",unsafe_allow_html=True)
    st.markdown(f"<div class='v4-card'><div class='v4-row'><div><div class='v4-kicker'>Financial score</div><div class='v4-title'>Your plan health</div></div><div class='v4-score'>{score}<span style='font-size:1rem;color:#8793a1'>/100</span></div></div></div>",unsafe_allow_html=True)
    st.markdown("<div class='v4-section'>Your journey</div>",unsafe_allow_html=True)
    goals=sorted(v4_goals(),key=lambda g:{'Critical':0,'High':1,'Normal':2,'Lifestyle':3}.get(g.get('priority'),2))
    if not goals: st.info('Your money has no destination yet. Add your first goal in Journey and Finance OS will start measuring pace, deadlines and recovery options.')
    for g in goals[:3]:
        s=v4_goal_status(g); pct=int(round(s['progress']*100)); ahead=s.get('ahead_amount'); pace='On pace' if ahead is None or abs(ahead)<25 else f"{money(abs(ahead))} {'ahead of' if ahead>0 else 'behind'} pace"; deadline=s['deadline'].strftime('%b %Y') if s.get('deadline') else 'No deadline'; cur=f"{money(s['current'])} remaining" if g.get('kind')=='debt' else money(s['current'])
        st.markdown(f"<div class='v4-card'><div class='v4-row'><div class='v4-title'>{g['name']}</div><div>{pct}%</div></div><div class='v4-progress'><span style='width:{pct}%'></span></div><div class='v4-row'><div>{cur}</div><div class='v4-muted'>{deadline}</div></div><div class='v4-muted'>{pace}</div></div>",unsafe_allow_html=True)
    st.markdown("<div class='v4-section'>Next best move</div>",unsafe_allow_html=True); st.markdown(f"<div class='v4-card'><div class='v4-title'>{rec_title}</div><div class='v4-muted'>{rec_detail}</div></div>",unsafe_allow_html=True)
    st.markdown("<div class='v4-section'>Coming up</div>",unsafe_allow_html=True)
    for e in plan.get('events',[])[:5]:
        d=e.get('date'); dt=d.strftime('%b %d') if hasattr(d,'strftime') else str(d); impact=float(e.get('cash_impact',0) or 0); shown=money(abs(impact)) if abs(impact)>.01 else money(float(e.get('amount',0) or 0))
        st.markdown(f"<div class='v4-card'><div class='v4-row'><div><b>{dt}</b> · {e.get('name','Event')}<div class='v4-muted'>{str(e.get('kind','')).replace('_',' ').title()}</div></div><div><b>{shown}</b></div></div></div>",unsafe_allow_html=True)

elif page=='Journey':
    st.markdown("<div class='v4-hero'><div class='v4-kicker'>Your financial GPS</div><div class='v4-sub'>Where you are → where you want to go</div><div class='v4-muted'>Original goals stay visible while Finance OS recalculates the fastest safe route when life changes.</div></div>",unsafe_allow_html=True)
    goals=v4_goals()
    for g in goals:
        s=v4_goal_status(g); pct=int(round(s['progress']*100)); ahead=s.get('ahead_amount'); status='ON TRACK' if ahead is None or ahead>=-25 else 'BEHIND PACE'; cls='v4-good' if status=='ON TRACK' else 'v4-warn'; proj=s.get('projected_date'); projtxt=proj.strftime('%b %d, %Y') if proj else 'Need more history'; need=s.get('required_per_paycheck')
        st.markdown(f"<div class='v4-card'><div class='v4-row'><div><span class='v4-pill'>{g.get('priority','Normal')}</span><div class='v4-title' style='margin-top:9px'>{g['name']}</div></div><div class='{cls}'><b>{status}</b></div></div><div class='v4-progress'><span style='width:{pct}%'></span></div><div class='v4-row'><div>{pct}% complete</div><div>{money(s['remaining'])} to go</div></div><div class='v4-muted'>Original deadline: {s['deadline'].strftime('%b %d, %Y') if s.get('deadline') else 'None'} · Current projection: {projtxt}</div>{f"<div class='v4-muted'>Needed from remaining paychecks: about {money(need)} each</div>" if need is not None else ''}</div>",unsafe_allow_html=True)
    with st.expander('＋ Add a goal',expanded=not bool(goals)):
        with st.form('v4_add_goal'):
            kind=st.selectbox('Goal type',['savings','emergency','debt','purchase']); name=st.text_input('Goal name',placeholder='Emergency fund, pay off card, new couch…'); target=st.number_input('Target amount',min_value=0.0,step=100.0); deadline=st.date_input('Target date',value=finance_today_v3()+timedelta(days=180),min_value=finance_today_v3()); priority=st.selectbox('Priority',['Critical','High','Normal','Lifestyle']); current=None; card=None
            if kind=='debt':
                opts=[c.get('card') for c in LIVE_CARDS if c.get('card')]; card=st.selectbox('Debt account',opts) if opts else None; current=next((float(c.get('balance',0) or 0) for c in LIVE_CARDS if c.get('card')==card),0); st.caption(f'Current balance: {money(current)}')
            elif kind=='purchase': current=st.number_input('Already saved for this',min_value=0.0,step=100.0)
            if st.form_submit_button('Create goal',use_container_width=True) and name.strip() and target>0: v4_add_goal(name,kind,target,deadline,priority,current,card)
    if goals:
        with st.expander('Manage goals'):
            remove=st.selectbox('Goal',[g['name'] for g in goals]);
            if st.button('Remove selected goal',use_container_width=True): STATE['goals']=[g for g in STATE.get('goals',[]) if g.get('name')!=remove]; save_and_reload('Finance OS 4.1: remove goal')

elif page=='Plan':
    st.markdown(f"<div class='v4-hero'><div class='v4-kicker'>Cash runway</div><div class='v4-big'>{money(plan['projected_before_payday'])}</div><div class='v4-sub'>Projected checking before {plan['next_payday'].strftime('%b %d')}</div><div class='v4-muted'>{money(plan['checking'])} now − {money(plan['known_cash_out'])} known cash out + {money(plan['known_cash_in'])} cash in</div></div>",unsafe_allow_html=True)
    st.markdown("<div class='v4-section'>Timeline</div>",unsafe_allow_html=True); running=float(plan['checking'])
    for e in plan.get('events',[]):
        running+=float(e.get('cash_impact',0) or 0); d=e.get('date'); dt=d.strftime('%b %d') if hasattr(d,'strftime') else str(d); impact=float(e.get('cash_impact',0) or 0)
        st.markdown(f"<div class='v4-card'><div class='v4-row'><div><b>{dt}</b> · {e.get('name','Event')}<div class='v4-muted'>{str(e.get('kind','')).replace('_',' ').title()}</div></div><div style='text-align:right'><b>{money(impact)}</b><div class='v4-muted'>{money(running)} after</div></div></div></div>",unsafe_allow_html=True)
    st.markdown("<div class='v4-section'>Plan a big expense</div>",unsafe_allow_html=True); purchase=st.number_input('What will it cost?',min_value=0.0,step=100.0,key='v4_big_purchase')
    if purchase>0:
        safe=max(0,float(plan.get('safe_to_spend',0) or 0)); shortage=max(0,purchase-safe); checks=max(1,int((shortage+499)//500)) if shortage else 0; best=plan['next_payday']+timedelta(days=max(0,(checks-1)*14))
        if purchase<=safe: st.success(f'You can fund {money(purchase)} from today’s modeled surplus without touching protected cash.')
        else: st.info(f'Waiting is healthier. You’re about {money(shortage)} short of funding this from today’s safe surplus. A starting target is {best.strftime("%b %d")}.' )
        if st.button('Turn this into a purchase goal',use_container_width=True): v4_add_goal(f'Big purchase · {money(purchase)}','purchase',purchase,best,'Lifestyle',0,None)
    st.markdown("<div class='v4-section'>Can I spend?</div>",unsafe_allow_html=True); test=st.number_input('Purchase amount',min_value=0.0,step=25.0,key='v4_can_spend')
    if test>0:
        safe=float(plan.get('safe_to_spend',0) or 0)
        if spend.get('safe') and test<=safe: st.success(f"Yes. You'd still have about {money(safe-test)} safe-to-spend capacity before {plan['next_payday'].strftime('%b %d')}.")
        elif not spend.get('safe'): st.warning("Not yet. A required payment is unresolved, so Finance OS won't pretend this purchase is safe.")
        else: st.warning(f"I'd wait. This is about {money(test-safe)} above today's safe-to-spend amount.")

elif page=='Money':
    checking=mapped_cash_balance_v3('checking'); savings=mapped_cash_balance_v3('savings'); debt=sum(max(0,float(c.get('balance',0) or 0)) for c in LIVE_CARDS)
    st.markdown(f"<div class='v4-hero'><div class='v4-kicker'>Money now</div><div class='v4-big'>{money(checking+savings)}</div><div class='v4-sub'>Cash across checking + savings</div></div>",unsafe_allow_html=True); a,b=st.columns(2); a.metric('Checking',money(checking)); b.metric('Savings',money(savings)); st.markdown(f"<div class='v4-section'>Credit · {money(debt)} owed</div>",unsafe_allow_html=True)
    for c in LIVE_CARDS:
        bal=max(0,float(c.get('balance',0) or 0)); lim=float(c.get('limit',0) or 0); util=bal/lim*100 if lim else 0; st.markdown(f"<div class='v4-card'><div class='v4-row'><div><div class='v4-title'>{c.get('card','Card')}</div><div class='v4-muted'>{util:.0f}% utilization</div></div><div style='text-align:right'><b>{money(bal)}</b><div class='v4-muted'>of {money(lim) if lim else 'limit unknown'}</div></div></div></div>",unsafe_allow_html=True)
    st.markdown("<div class='v4-section'>Recent activity</div>",unsafe_allow_html=True); txs=sorted(STATE.get('plaid',{}).get('transactions',[]) or [],key=lambda x:str(x.get('date','')),reverse=True)[:12]
    for t in txs:
        nm=t.get('merchant_name') or t.get('name') or 'Transaction'; ds=str(t.get('date',''))[:10]; amt=float(t.get('amount',0) or 0); st.markdown(f"<div class='v4-card'><div class='v4-row'><div><b>{nm}</b><div class='v4-muted'>{ds}</div></div><div><b>{money(amt)}</b></div></div></div>",unsafe_allow_html=True)

elif page=='More':
    session_label = "DEMO · SYNTHETIC DATA" if st.session_state.get("fos_v4_mode") == "demo" else "PREVIEW · SESSION ONLY" if st.session_state.get("fos_v4_mode") == "personal_preview" else "CONTROL ROOM"
    st.markdown(f"<div class='v4-hero'><div class='v4-kicker'>{session_label}</div><div class='v4-sub'>Settings stay out of everyday money decisions.</div><div class='v4-muted'>This public beta never needs your real banking credentials.</div></div>",unsafe_allow_html=True)
    if st.button('Exit to welcome',use_container_width=True):
        st.session_state.fos_v4_mode='welcome'; st.session_state.pop('live_state',None); st.rerun()
    st.markdown("<div class='v4-section'>Score breakdown</div>",unsafe_allow_html=True)
    for k,v in score_parts.items(): st.markdown(f"<div class='v4-card'><div class='v4-row'><div>{k}</div><div><b>{v}/10</b></div></div></div>",unsafe_allow_html=True)
    st.markdown("<div class='v4-section'>Accounts</div>",unsafe_allow_html=True)
    if st.session_state.get("fos_v4_mode") in {"demo", "personal_preview"}:
        st.info("Bank connection is intentionally disabled in this public test. Demo data is synthetic; preview data lives only in this browser session.")
    else:
        if st.button('Refresh bank data',use_container_width=True):
            try: refresh_all_plaid_v3(); save_and_reload('Finance OS 4.1: refresh banks')
            except Exception as exc: st.error(f'Bank refresh failed: {exc}')
        st.caption(f"Plaid: {'connected' if STATE.get('plaid',{}).get('items') else 'not connected'} · Last sync: {STATE.get('plaid',{}).get('last_sync') or 'Never'}")
    with st.expander('System health & diagnostics'):
        issues=data_health_v3();
        if not issues: st.success('Core checks passed.')
        for issue in issues: st.write(f"**{issue.get('title')}**"); st.caption(issue.get('detail',''))
        st.write(f"Persistent state: {'Ready' if github_settings() else 'Needs setup'}"); st.write(f"Plaid token encryption: {'Ready' if plaid_crypto_ready() else 'Needs setup'}")
    with st.expander('Card payment rules'):
        for row in card_payment_rules_summary_v3():
            name=row.get('card','Card'); rule=row.get('rule') or {}; suggestion=row.get('suggestion') or {}; st.markdown(f"**{name}** · {money(row.get('balance',0))}"); med=suggestion.get('median_amount'); day=suggestion.get('median_day');
            if med is not None: st.caption(f"History suggests around {money(med)}" + (f" near day {day}." if day is not None else '.'))
            st.caption(f"Rule: {rule.get('mode','unknown')} · Due day: {rule.get('due_day') or 'unknown'} · {'confirmed' if rule.get('confirmed') else 'needs confirmation'}")

st.caption(f'Finance OS {APP_VERSION} · Journey-first · deterministic ledger underneath')
