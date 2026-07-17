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

# Shared portal relay inboxes: many DIFFERENT leads arrive under the same address,
# so these must NEVER be used as a dedupe key (would merge unrelated people).
RELAY_EMAILS = {
    "leads@email.realtor.com", "mail@e.rent.com", "noreply@mail.rentvine.com",
    "notifications@usehaven.ai",
}

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
    # includeArchived=true is essential: dedupe must see archived/completed cards,
    # otherwise a previously-archived lead is treated as brand-new and re-created.
    cards, page = [], 0
    while True:
        d = http(f"{AP_BASE}?page={page}&pageSize=200&includeArchived=true", {"x-token": AP_TOKEN})
        batch = d.get("data", [])
        if not batch: break
        cards += batch
        if len(batch) < 200: break
        page += 1
    seen = set()
    return [c for c in cards if not (c["_id"] in seen or seen.add(c["_id"]))]

def norm_addr(s):
    """Normalize an address for comparison: lowercase, expand/strip abbreviations,
    drop punctuation and extra whitespace. 'St' == 'Street', '#123' == 'Unit 123'."""
    import re as _re
    s = (s or "").lower()
    s = s.replace("#", "unit ")
    s = _re.sub(r"[.,]", " ", s)
    words = {
        "st": "street", "ave": "avenue", "av": "avenue", "blvd": "boulevard",
        "dr": "drive", "ln": "lane", "ct": "court", "rd": "road", "pl": "place",
        "trl": "trail", "tr": "trail", "cir": "circle", "pkwy": "parkway",
        "hwy": "highway", "ter": "terrace", "sq": "square", "apt": "unit",
        "ste": "unit", "suite": "unit", "bldg": "building", "n": "north",
        "s": "south", "e": "east", "w": "west",
    }
    out = [words.get(w, w) for w in s.split()]
    return _re.sub(r"\s+", " ", " ".join(out)).strip()

def ap_post(body):
    return http(AP_BASE, {"x-token": AP_TOKEN}, body)

# Author for auto-generated comments (first company user). Resolved lazily.
COMMENT_USER_ID = None
def _resolve_comment_user():
    global COMMENT_USER_ID
    try:
        u = http("https://core-api.getaptly.com/api/users", {"x-token": AP_TOKEN})
        users = u.get("data", u) if isinstance(u, dict) else u
        if isinstance(users, list) and users:
            COMMENT_USER_ID = users[0].get("_id") or users[0].get("userId")
    except Exception:
        COMMENT_USER_ID = None

def _create_card(base, name, email, phone, by_email, by_phone, by_name):
    """Create a card in 'New Lead Received', link a contact, and register it in the
    in-memory indexes so later prospects in the same run dedupe against it."""
    r = ap_post({**base, "stage": "New Lead Received"})
    new_id = r["data"]["_id"]
    try:
        cid = ap_upsert_contact(name, email, phone)
        duo = "".join(w[0] for w in name.split()[:2]).upper() or "??"
        if cid:
            ap_post({"_id": new_id, "contact": {"_id": cid, "name": name, "duogram": duo}})
    except Exception as ex:
        print(f"contact link failed for {email}: {ex}")
    # register in indexes (mirrors the fields find_existing_cards / card_unit_norm read)
    rec = {"_id": new_id, "createdAt": "zzz", "archived": False,
           "trackingEmail": email, "trackingPhone": phone, "name": name,
           "unit": base.get("unit"), "description": base.get("description")}
    d = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    if email and email.strip().lower() not in RELAY_EMAILS:
        by_email.setdefault(email, []).append(rec)
    if d and len(d) == 10: by_phone.setdefault(d, []).append(rec)
    if name: by_name.setdefault(name.strip().lower(), []).append(rec)
    return new_id


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
    _resolve_comment_user()
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

    # 3) fetch ALL Aptly cards (active + archived) and index them by email, phone, name.
    #    Each index maps a key -> list of that person's cards, so we can check every
    #    property they already have on record (active OR archived).
    cards = ap_get_cards()
    archived_ct = sum(1 for c in cards if c.get("archived"))
    by_card_email, by_card_phone, by_card_name = {}, {}, {}
    def _digits(p): return "".join(ch for ch in (p or "") if ch.isdigit())[-10:]
    for c in cards:
        if (c.get("name") or "").startswith("DELETE"):
            continue  # ignore our own tombstoned cards
        e = (c.get("trackingEmail") or "").strip().lower()
        if e in RELAY_EMAILS: e = ""  # never index by a shared relay inbox
        ph = _digits(c.get("trackingPhone"))
        nm = (c.get("name") or "").strip().lower()
        if e: by_card_email.setdefault(e, []).append(c)
        if ph and len(ph) == 10: by_card_phone.setdefault(ph, []).append(c)
        if nm: by_card_name.setdefault(nm, []).append(c)

    def card_unit_norm(c):
        # Prefer the linked property ref; fall back to the "Unit of interest:" line
        # in the description (covers synced cards whose address didn't resolve to a
        # linked Aptly property, so re-runs still recognize the property on record).
        u = c.get("unit")
        if isinstance(u, dict) and u.get("name"): return norm_addr(u["name"])
        desc = c.get("description") or ""
        for line in desc.splitlines():
            if line.lower().startswith("unit of interest:"):
                val = line.split(":", 1)[1].strip()
                # keep only the street part (before the first em-dash separator)
                val = val.split(" — ")[0].strip()
                if val and val.lower() != "n/a":
                    return norm_addr(val)
        return None

    def find_existing_cards(email, phone, name):
        """Return the person's matching cards, trying email, then phone, then name.
        Relay/shared inboxes are skipped as an email key (they aren't a person)."""
        e = (email or "").strip().lower()
        if e and e not in RELAY_EMAILS and e in by_card_email:
            return by_card_email[e]
        d = _digits(phone)
        if d and len(d) == 10 and d in by_card_phone: return by_card_phone[d]
        n = (name or "").strip().lower()
        if n and n in by_card_name: return by_card_name[n]
        return []

    print(f"Aptly cards: {len(cards)} (archived: {archived_ct}) | "
          f"indexed by email: {len(by_card_email)}, phone: {len(by_card_phone)}, name: {len(by_card_name)}")
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
        phone = primary.get("phone") or ""
        name = (primary.get("name") or email).strip()
        # normalized address of the property THIS inquiry is about.
        # Use street-only (before the " — beds/baths/rent" suffix) so it matches
        # exactly what card_unit_norm extracts when reading a card back later.
        if unit_ref:
            new_unit_norm = norm_addr(unit_ref["name"])
        elif main_unit:
            new_unit_norm = norm_addr(main_unit.split(" — ")[0].strip())
        else:
            new_unit_norm = None
        base = {
            "name": name,
            "trackingEmail": email,
            "trackingPhone": phone,
            "leadSource": primary.get("leadSource") or "Rentvine",
            "description": desc,
        }
        if unit_ref: base["unit"] = unit_ref

        # ---- Match against ALL cards (active + archived) by email > phone > name ----
        matches = find_existing_cards(email, phone, name)
        prior_units = {u for c in matches if (u := card_unit_norm(c))}
        # pick the most recent NON-archived match to update in place if we have one
        active_matches = [c for c in matches if not c.get("archived")]
        active_matches.sort(key=lambda c: c.get("createdAt", ""), reverse=True)

        try:
            # RULE 1: no match anywhere -> brand-new lead, create normally
            if not matches:
                if DRY:
                    print(f"[dry] CREATE (new) {name} <{email}> | {main_unit}")
                    created += 1
                else:
                    _create_card(base, name, email, phone, by_card_email, by_card_phone, by_card_name)
                    created += 1
                time.sleep(0.15)
                continue

            # A genuinely different property requires BOTH: this inquiry names a real
            # property, AND at least one prior card names a DIFFERENT real property.
            # If prior cards have no property on record (or this inquiry has none),
            # it is NOT a new-property inquiry — treat as the same lead (update in place).
            different_property = (
                new_unit_norm is not None
                and len(prior_units) > 0
                and new_unit_norm not in prior_units
            )
            same_property = not different_property

            # RULE 2: match exists AND same/not-different property (even if archived) -> duplicate
            if same_property:
                # If there's an active card, keep it fresh (description/contact) but never
                # create a second card and never change its stage.
                if active_matches:
                    ex_card = active_matches[0]
                    need = (ex_card.get("description") or "") != desc or (ex_card.get("name") or "") != name
                    if need and not DRY:
                        ap_post({**base, "_id": ex_card["_id"]})
                        updated += 1
                    else:
                        unchanged += 1
                else:
                    # only archived copies exist for this same property -> leave alone
                    unchanged += 1
                time.sleep(0.05)
                continue

            # RULE 3: match exists but DIFFERENT property -> existing lead, new inquiry.
            # Create a new card for the new property and add an explanatory comment.
            prior_list = "; ".join(sorted(prior_units)) or "(no property on record)"
            if DRY:
                print(f"[dry] CREATE (new inquiry) {name} <{email}> | new: {main_unit} | prior: {prior_list}")
                created += 1
            else:
                new_id = _create_card(base, name, email, phone, by_card_email, by_card_phone, by_card_name)
                comment = (f"This lead has previously inquired (see existing/archived card). "
                           f"They are now inquiring about a new property: {main_unit or 'n/a'}. "
                           f"Prior inquiry on record: {prior_list}.")
                try:
                    if new_id and COMMENT_USER_ID:
                        http(f"{AP_BASE}/{new_id}/comment", {"x-token": AP_TOKEN},
                             {"userId": COMMENT_USER_ID, "content": comment})
                except Exception as ex:
                    print(f"comment failed for {email}: {ex}")
                created += 1
            time.sleep(0.15)

        except Exception as ex:
            failed += 1
            print(f"FAIL {email}: {ex}")

    print(f"\n{'DRY RUN — ' if DRY else ''}created: {created} | updated: {updated} | unchanged: {unchanged} | failed: {failed}")

if __name__ == "__main__":
    main() 
