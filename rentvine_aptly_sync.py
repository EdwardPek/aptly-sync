#!/usr/bin/env python3
"""Rentvine -> Aptly prospect sync.
Pulls screening prospects (new / invitation_sent / application_started) from Rentvine
and creates/updates cards on the Aptly Renter Leads board. Dedupe is by email:
one card per person, extra units of interest are appended to the description.

Env vars: RENTVINE_KEY, RENTVINE_SECRET, APTLY_TOKEN. Optional: DRY_RUN=1
"""
import json, os, sys, time, urllib.request, base64
from collections import defaultdict

RV_BASE = "https://virtualhomesrealty.rentvine.com/api/manager/screening/prospects"
AP_BASE = "https://core-api.getaptly.com/api/board/PDTjqKvgJyadH62P4"
STATUSES = ["new", "invitation_sent", "application_started"]
DRY = os.environ.get("DRY_RUN") == "1"

RV_AUTH = "Basic " + base64.b64encode(f"{os.environ['RENTVINE_KEY']}:{os.environ['RENTVINE_SECRET']}".encode()).decode()
AP_TOKEN = os.environ["APTLY_TOKEN"]

def http(url, headers, body=None, method=None):
    req = urllib.request.Request(url, headers={**headers, "User-Agent": "curl/8.5.0", "Accept": "application/json"}, method=method or ("POST" if body else "GET"),
                                 data=json.dumps(body).encode() if body else None)
    if body: req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def rv_get(page):
    qs = f"page={page}&pageSize=100&" + "&".join(f"statuses[{i}]={s}" for i, s in enumerate(STATUSES))
    return http(f"{RV_BASE}?{qs}", {"Authorization": RV_AUTH})

def ap_get_cards():
    cards, page = [], 0
    while True:
        d = http(f"{AP_BASE}?page={page}&pageSize=200", {"x-token": AP_TOKEN})
        batch = d.get("data", [])
        if not batch: break
        cards += batch
        if len(batch) < 200: break
        page += 1
    seen = set()
    return [c for c in cards if not (c["_id"] in seen or seen.add(c["_id"]))]

def ap_post(body):
    return http(AP_BASE, {"x-token": AP_TOKEN}, body)


def ap_upsert_contact(name, email, phone):
    """Create/update an Aptly contact (matched by email) and return its _id."""
    parts = (name or email).strip().split()
    body = {"First Name": parts[0], "Last Name": " ".join(parts[1:]) or "-",
            "Contact Type": "Tenant Prospect", "Email": email}
    if phone: body["Mobile Phone"] = phone
    r = http("https://core-api.getaptly.com/api/contacts", {"x-token": AP_TOKEN}, body)
    return r.get("_id")

def unit_line(rec):
    u = rec.get("unit") or {}
    if not u.get("address"): return None
    baths = float(u.get("fullBaths") or 0) + 0.5 * float(u.get("halfBaths") or 0)
    baths = int(baths) if baths == int(baths) else baths
    rent = f"${float(u['rent']):,.0f}/mo" if u.get("rent") else ""
    parts = [f"{u['address']}, {u.get('city','')}, {u.get('stateID','')} {u.get('postalCode','')}".strip()]
    if u.get("beds"): parts.append(f"{u['beds']} bd / {baths} ba")
    if rent: parts.append(rent)
    return " — ".join(parts)



def load_unit_map():
    """Load the Rentvine->Aptly unit map (aptly_units_map.json, shipped with this script)."""
    import re as _re
    def norm(s): return _re.sub(r"\s+"," ",_re.sub(r"[#().]","",_re.sub(r"\b(ste|suite|unit|apt|uni|bldg)\b","u",(s or "").lower()))).strip()
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"aptly_units_map.json")
    by_unit,by_street={},{}
    try:
        M=json.load(open(path))
        by_unit={norm(v["street"]+" "+v["desig"]):v for v in M.get("byUnit",{}).values()}
        by_street={norm(k):v for k,v in M.get("byStreet",{}).items()}
    except Exception as ex:
        print(f"unit map not loaded ({ex}); Preferred Rental will rely on board-learned links only")
    return norm,by_unit,by_street

def resolve_unit(known, rv_unit_obj, _cache={}):
    """Resolve a Rentvine unit to an Aptly property ref via the map, board-learned refs as fallback."""
    if "n" not in _cache:
        _cache["n"],_cache["u"],_cache["s"]=load_unit_map()
    if not rv_unit_obj or not rv_unit_obj.get("address"): return None
    norm,by_unit,by_street=_cache["n"],_cache["u"],_cache["s"]
    a=(rv_unit_obj.get("address") or "").strip(); a2=(rv_unit_obj.get("address2") or "").strip()
    v=by_unit.get(norm((a+" "+a2).strip()))
    if v:
        name=v["name"]
    else:
        v=by_street.get(norm(a))
        name=(v["name"] if v and not a2 else (a+" "+a2).strip()) if v else None
    if v:
        duo="".join(w[0] for w in name.split()[:2]).upper() or "??"
        return {"name":name,"_id":v["aptlyId"],"duogram":duo}
    k=norm((a+" "+a2).strip())
    if k in known:
        nm,_id=known[k]
        duo="".join(w[0] for w in nm.split()[:2]).upper() or "??"
        return {"name":nm,"_id":_id,"duogram":duo}
    return None  # never write name-only values: they render as "Unit not found"

def main():
    # 1) fetch all matching prospects from Rentvine
    prospects, page = [], 1
    while True:
        batch = rv_get(page)
        if not batch: break
        prospects += batch
        if len(batch) < 100: break
        page += 1
    print(f"Rentvine prospects fetched: {len(prospects)}")

    # 2) group by email (one card per person)
    by_email = defaultdict(list)
    skipped_no_email = 0
    for rec in prospects:
        email = (rec["prospect"].get("email") or "").strip().lower()
        if not email:
            skipped_no_email += 1
            continue
        by_email[email].append(rec)
    print(f"Unique people (by email): {len(by_email)} | records without email skipped: {skipped_no_email}")

    # 3) fetch Aptly board, map email -> existing card (prefer non-DELETE, most recent)
    cards = ap_get_cards()
    card_by_email = {}
    for c in cards:
        e = (c.get("trackingEmail") or "").strip().lower()
        if not e: continue
        cur = card_by_email.get(e)
        if cur is None or (c.get("createdAt", "") > cur.get("createdAt", "")):
            card_by_email[e] = c
    print(f"Aptly cards on board: {len(cards)} | with email: {len(card_by_email)}")
    _n,_,_=load_unit_map()
    known = {}
    for c in cards:
        u = c.get("unit")
        if isinstance(u, dict) and u.get("_id") and u.get("name"):
            known[_n(u["name"])] = (u["name"], u["_id"])

    created = updated = unchanged = failed = 0
    for email, recs in by_email.items():
        recs.sort(key=lambda r: r["prospect"].get("dateTimeModified") or "", reverse=True)
        primary = recs[0]["prospect"]
        lines = []
        main_unit = unit_line(recs[0])
        lines.append(f"Unit of interest: {main_unit or 'n/a'}")
        extra_units = sorted({ul for r in recs[1:] if (ul := unit_line(r)) and ul != main_unit})
        if extra_units:
            lines.append("Also interested in: " + "; ".join(extra_units))
        lines.append(f"Rentvine status: {primary.get('status')}")
        lines.append(f"Rentvine ID: {primary.get('prospectID')}")
        lines.append(f"Lead source: {primary.get('leadSource') or 'Rentvine'}")
        lines.append("Synced from Rentvine")
        desc = "\n".join(lines)

        unit_ref = resolve_unit(known, recs[0].get("unit"))
        base = {
            "name": (primary.get("name") or email).strip(),
            "trackingEmail": email,
            "trackingPhone": primary.get("phone") or "",
            "leadSource": primary.get("leadSource") or "Rentvine",
            "description": desc,
        }
        if unit_ref: base["unit"] = unit_ref
        existing = card_by_email.get(email)
        try:
            if existing:
                if (existing.get("description") or "") == desc and (existing.get("name") or "") == base["name"] \
                        and (not unit_ref or (isinstance(existing.get("unit"),dict) and existing["unit"].get("_id")==unit_ref.get("_id"))):
                    unchanged += 1
                    continue
                if DRY:
                    print(f"[dry] UPDATE {base['name']} <{email}>")
                else:
                    ap_post({**base, "_id": existing["_id"]})  # no stage on updates
                    if not existing.get("contact"):
                        try:
                            cid = ap_upsert_contact(base["name"], email, base["trackingPhone"])
                            duo = "".join(w[0] for w in base["name"].split()[:2]).upper() or "??"
                            if cid: ap_post({"_id": existing["_id"], "contact": {"_id": cid, "name": base["name"], "duogram": duo}})
                        except Exception as ex:
                            print(f"contact link failed for {email}: {ex}")
                updated += 1
            else:
                if DRY:
                    print(f"[dry] CREATE {base['name']} <{email}> | {main_unit}")
                else:
                    r = ap_post({**base, "stage": "Nurturing"})
                    try:
                        cid = ap_upsert_contact(base["name"], email, base["trackingPhone"])
                        duo = "".join(w[0] for w in base["name"].split()[:2]).upper() or "??"
                        if cid: ap_post({"_id": r["data"]["_id"], "contact": {"_id": cid, "name": base["name"], "duogram": duo}})
                    except Exception as ex:
                        print(f"contact link failed for {email}: {ex}")
                    card_by_email[email] = {"_id": r["data"]["_id"], "createdAt": "now", "description": desc, "name": base["name"]}
                created += 1
            time.sleep(0.15)
        except Exception as ex:
            failed += 1
            print(f"FAIL {email}: {ex}")

    print(f"\n{'DRY RUN — ' if DRY else ''}created: {created} | updated: {updated} | unchanged: {unchanged} | failed: {failed}")

if __name__ == "__main__":
    main()
