"""Latest shareholding pattern and promoter pledge for an NSE-listed company, from NSE's own filings.

Sources (all free, no login):
- NSE's list of a company's quarterly shareholding-pattern filings (www.nseindia.in/api/corporate-share-holdings-master),
  each with the company's XBRL file on nsearchives;
- the XBRL file itself (promoters, foreign and domestic institutions, mutual funds, insurers, government, retail,
  and promoter shares pledged or otherwise encumbered);
- NSE's promoter-pledge disclosure (www.nseindia.in/api/corporate-pledgedata, SEBI SAST Regulation 31 data),
  used as a cross-check.

Research only. Nothing here is a recommendation.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import http.client
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from core import Block, instant
from public_market import NoRedirect, LIMIT

API_HOST = "www.nseindia.in"
API_PATHS = {"/api/corporate-share-holdings-master", "/api/corporate-pledgedata", "/api/corporates-corporateActions",
             "/api/integrated-filing-results", "/api/corporate-sast-reg29", "/api/corporates-financial-results",
             "/api/corporate-announcements"}  # announcements: equity allotted after the latest shareholding pattern (a QIP)
# Extra fixed query parameters some endpoints need (the annual results list: period=Annual).
FIXED_PARAMS = {"/api/corporates-financial-results": {"period": "Annual"}}
ARCHIVE_HOSTS = {"nsearchives.nseindia.in", "nsearchives.nseindia.com"}
SOURCE_NOTE = ("NSE shareholding-pattern filings (company XBRL on nsearchives) and NSE's promoter-pledge disclosure "
               "(SEBI SAST Regulation 31)")


PAGED_PATHS = {"/api/integrated-filing-results"}  # 20 rows a page; older results sit on later pages
# NSE's bulk- and block-deal history for one symbol over at most a year (dates dd-mm-yyyy), read only to name the
# promoter entities behind a large promoter move that has no SAST filing.
DEALS_PATH = "/api/historicalOR/bulk-block-short-deals"
DEAL_KINDS = ("bulk_deals", "block_deals")


def api_url(path, symbol, page=None):
    return (f"https://{API_HOST}{path}?" + urllib.parse.urlencode({"index": "equities", "symbol": symbol, **FIXED_PARAMS.get(path, {})})
            + (f"&page={page}" if page else ""))


def deals_url(kind, symbol, start, end):
    """NSE's bulk- or block-deal history for one symbol between two dates."""
    return (f"https://{API_HOST}{DEALS_PATH}?" + urllib.parse.urlencode({"optionType": kind, "symbol": symbol,
                                                                         "from": start.strftime("%d-%m-%Y"), "to": end.strftime("%d-%m-%Y")}))


def _deals_allowed(url, query):
    q = urllib.parse.parse_qs(query)
    if set(q) != {"optionType", "symbol", "from", "to"} or any(len(v) != 1 for v in q.values()):
        return False
    kind, symbol, start, end = q["optionType"][0], q["symbol"][0], q["from"][0], q["to"][0]
    if kind not in DEAL_KINDS or not re.fullmatch(r"[A-Z0-9&_.-]{1,30}", symbol):
        return False
    try:
        a, b = datetime.strptime(start, "%d-%m-%Y").date(), datetime.strptime(end, "%d-%m-%Y").date()
    except ValueError:
        return False
    return 0 <= (b - a).days <= 366 and deals_url(kind, symbol, a, b) == url


def allowed(url):
    """Only NSE's company endpoints listed above (exact query), the bulk/block-deal history of one symbol, and
    shareholding / results XBRL files on NSE's archive."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.fragment:
        return False
    if p.hostname == API_HOST and p.path == DEALS_PATH:
        return _deals_allowed(url, p.query)
    if p.hostname == API_HOST and p.path in API_PATHS:
        q = urllib.parse.parse_qs(p.query)
        page = q.get("page", [None])
        if "page" in q and (p.path not in PAGED_PATHS or len(page) != 1 or not re.fullmatch(r"[2-9]", page[0])):
            return False
        fixed = FIXED_PARAMS.get(p.path, {})
        return (set(q) - {"page"} == {"index", "symbol", *fixed} and q["index"] == ["equities"] and len(q["symbol"]) == 1
                and all(q.get(k) == [v] for k, v in fixed.items())
                and re.fullmatch(r"[A-Z0-9&_.-]{1,30}", q["symbol"][0]) is not None and api_url(p.path, q["symbol"][0], page[0]) == url)
    # Results XBRL names carry the taxonomy: INDAS (companies), NBFC_INDAS, BANKING, LI / GI (insurers); the older
    # annual results files (FY2019-FY2024) have no INTEGRATED_FILING_ prefix.
    return (p.hostname in ARCHIVE_HOSTS
            and re.fullmatch(r"/corporate/xbrl/(?:SHP|(?:INTEGRATED_FILING_)?(?:INDAS|NBFC_INDAS|BANKING|LI|GI))_[A-Za-z0-9_]+\.xml", p.path or "") is not None
            and not p.query)


NSE_TIMEOUT = 15          # seconds per network operation; a stalled NSE call gives up instead of hanging
RETRY_DELAYS = (0.5, 1.5)  # waits before the 2nd and 3rd attempts (NSE often drops the first call of a burst)


def _transient(error):
    """Worth another try: a timeout, a dropped connection, a body cut short, HTTP 403/429 (NSE throttling) or a 5xx.
    A refused redirect or a 4xx such as 404 is final."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code in (403, 429) or 500 <= error.code <= 599
    return isinstance(error, (OSError, http.client.HTTPException))


def _open(req, timeout):
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
        return response.read(LIMIT + 1)


def nse_request(url, *, timeout=NSE_TIMEOUT, attempts=3, sleep=time.sleep, opener=None):
    """Allowlisted NSE fetch: no redirects, size-capped, plain identifying user agent. Transient failures are retried
    twice (after 0.5 s and 1.5 s); a run that still fails raises Block("NSE: source unavailable")."""
    if not allowed(url):
        raise Block("NSE: unsupported endpoint")
    req = urllib.request.Request(url, headers={"User-Agent": "IndiaDayTrader personal research",
                                               "Accept": "application/json,application/xml,text/xml"})
    opener = opener or _open
    for attempt in range(max(1, attempts)):
        try:
            body = opener(req, timeout)
            break
        except Block:
            raise  # a refused redirect
        except (OSError, ValueError, http.client.HTTPException) as error:
            if not _transient(error) or attempt >= attempts - 1:
                raise Block("NSE: source unavailable") from None
            sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
    if len(body) > LIMIT:
        raise Block("NSE: oversized response")
    return body


def nse_day(text):
    """'30-SEP-2026', '30-Sep-2026' or '17-Nov-2026 16:31:09' -> date."""
    text = (text or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y"):
        try:
            return datetime.strptime(text.title() if fmt == "%d-%b-%Y" else text[:11].title() + text[11:], fmt).date()
        except ValueError:
            continue
    return None


QUARTER_ENDS = {(3, 31), (6, 30), (9, 30), (12, 31)}


def _entry(r, day):
    filed = nse_day(r.get("broadcastDate")) or nse_day(r.get("submissionDate"))
    return {"quarter": day.isoformat(), "filed": filed.isoformat() if filed else None, "xbrl": r.get("xbrl") or None,
            "promoter_pct": _float(r.get("pr_and_prgrp")), "public_pct": _float(r.get("public_val")),
            "employee_trusts_pct": _float(r.get("employeeTrusts")), "name": r.get("name"),
            "revised": str(r.get("revisedData") or "").upper() == "Y"}


def _listing_rows(raw):
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise Block("SHAREHOLDING: unexpected NSE filing list")
    return [r for r in rows if isinstance(r, dict)]


def filings(raw, *, quarterly=True):
    """Shareholding filings, newest first, one per date (a revision replaces the original).

    quarterly=True keeps the quarter-end patterns; quarterly=False keeps the others: patterns filed on listing or for
    a share issue (pre-issue / ad-hoc), which are all a newly listed company has until its first quarter end."""
    best = {}
    for r in _listing_rows(raw):
        day = nse_day(r.get("date"))
        if not day or ((day.month, day.day) in QUARTER_ENDS) != quarterly:
            continue
        entry = _entry(r, day)
        key = entry["quarter"]
        if key not in best or (entry["filed"] or "") >= (best[key]["filed"] or ""):
            best[key] = entry
    return sorted(best.values(), key=lambda e: e["quarter"], reverse=True)


def next_quarter_due(day):
    """(next quarter end after `day`, the 21-day filing deadline) as ISO dates."""
    d = date.fromisoformat(day)
    for year in (d.year, d.year + 1):
        for month, dom in sorted(QUARTER_ENDS):
            end = date(year, month, dom)
            if end > d:
                return end.isoformat(), (end + timedelta(days=21)).isoformat()
    return None, None


def _float(text):
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value


def local(tag):
    return tag.rsplit("}", 1)[-1]


# XBRL category members (CategoryOfShareholdersAxis) -> our keys
MEMBERS = {
    "ShareholdingPatternMember": "total", "ShareholdingOfPromoterAndPromoterGroupMember": "promoter",
    "PublicShareholdingMember": "public", "InstitutionsForeignMember": "fii", "ForeignDirectInvestmentMember": "fdi",
    "InstitutionsForeignPortfolioInvestorCategoryOneMember": "fpi_1", "InstitutionsForeignPortfolioInvestorCategoryTwoMember": "fpi_2",
    "InstitutionsDomesticMember": "dii", "InstitutionsMember": "institutions_old", "MutualFundsOrUTIMember": "mf",
    "InsuranceCompaniesMember": "insurance", "GovernmentsMember": "government", "NonInstitutionsMember": "non_institutions",
    "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember": "small_individuals",
    "ResidentIndividualShareholdersHoldingNominalShareCapitalInExcessOfRsTwoLakhMember": "large_individuals",
    "SharesHeldByNonPromoterNonPublicShareholdersMember": "non_promoter_non_public", "EmployeeBenefitsTrustsMember": "employee_trusts",
    # The government's own lines: inside the promoter group (Coal India: the President of India, 61.13%) and in the public
    # category (Vodafone Idea: the Central Government, 49%), apart from domestic institutions.
    "CentralGovernmentOrStateGovernmentSMember": "promoter_government", "CentralGovernmentOrPresidentOfIndiaMember": "central_government",
    "StateGovernmentsOrGovernorsMember": "state_governments",
    "ShareholdingByCompaniesOrBodiesCorporateWhereCentralOrStateGovernmentIsPromoterMember": "government_companies",
}
FACTS = {"NumberOfShares": "shares", "ShareholdingAsAPercentageOfTotalNumberOfShares": "fraction", "NumberOfShareholders": "holders",
         "NumberOfSharesEncumberedUnderPledged": "pledged", "NumberOfSharesEncumbered": "encumbered",
         # Filings before mid-2025 (the older BSE-style taxonomy) give pledged and otherwise encumbered shares as one figure.
         "PledgedOrEncumberedNumberOfShares": "encumbered"}
FLAGS = {"WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged": "pledged",
         "WhetherAnySharesHeldByPromotersAreEncumberedUnderNonDisposalUndertaking": "non_disposal",
         "WhetherAnySharesHeldByPromotersAreEncumberedOtherThanByWayOfPledgeOrNDU": "other"}


def parse_xbrl(body):
    """Category holdings and promoter encumbrance from one shareholding-pattern XBRL instance."""
    if not isinstance(body, bytes) or not body or len(body) > LIMIT or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise Block("SHAREHOLDING: oversized or entity-bearing XML refused")
    root = ET.fromstring(body)
    if local(root.tag) != "xbrl":
        raise Block("SHAREHOLDING: expected an XBRL instance")
    contexts, instants = {}, {}
    for ctx in root.iter():
        if local(ctx.tag) != "context":
            continue
        members = [(e.attrib.get("dimension", ""), (e.text or "").strip()) for e in ctx.iter() if local(e.tag) == "explicitMember"]
        typed = any(local(e.tag) == "typedMember" for e in ctx.iter())
        moment = next(((e.text or "").strip() for e in ctx.iter() if local(e.tag) == "instant"), None)
        instants[ctx.attrib.get("id")] = moment
        if len(members) == 1 and not typed and members[0][0].endswith("CategoryOfShareholdersAxis"):
            name = members[0][1].rsplit(":", 1)[-1]
            if name in MEMBERS:
                contexts[ctx.attrib["id"]] = MEMBERS[name]
    named = named_contexts(root)
    cats, info, flags = {}, {}, {}
    holder_facts, sbo_facts, notes = {}, {}, None
    for e in root.iter():
        tag, ref = local(e.tag), e.attrib.get("contextRef")
        text = (e.text or "").strip()
        if ref in named and text and tag in HOLDER_FACTS:
            holder_facts.setdefault(named[ref], {})[HOLDER_FACTS[tag]] = text
            continue
        if ref in named and text and tag.startswith("CategoryOf"):
            # Older filings name a category row after its category ('HUF' under 'CategoryOfOtherNonInstitutions').
            holder_facts.setdefault(named[ref], {})["category_name"] = text
            continue
        if ref in named and text and tag in SBO_FACTS:
            sbo_facts.setdefault(named[ref], {})[SBO_FACTS[tag]] = text
            continue
        if tag == "DisclosureOfNotesOnShareholdingPatternExplanatoryTextBlock" and text and notes is None:
            notes = text
        if ref in contexts and tag in FACTS and text:
            try:
                value = float(text)
            except ValueError:
                continue
            cats.setdefault(contexts[ref], {})[FACTS[tag]] = value
        elif tag in FLAGS and ref == "MainI":
            flags[FLAGS[tag]] = flags.get(FLAGS[tag], False) or text.lower() in ("true", "1")
        elif tag in ("DateOfReport", "Symbol", "NameOfTheCompany", "ScripCode", "ISIN") and text:
            info.setdefault(tag, text)
    if "total" not in cats:
        raise Block("SHAREHOLDING: XBRL has no total holdings")
    if "promoter" not in cats and "public" not in cats:
        raise Block("SHAREHOLDING: XBRL has no promoter or public holdings")
    # Professionally managed companies have no promoter group at all (Eternal, HDFC Bank, ITC): a zero promoter row,
    # not an unreadable filing.
    cats.setdefault("promoter", {"shares": 0.0, "fraction": 0.0})
    total = cats["total"].get("shares")
    # Filings of early/mid 2025 (Bharti Airtel, March and June 2025) give "percentage" facts as percentages (52.42), not
    # fractions (0.5242): the total row says which.
    scale = 0.01 if (cats["total"].get("fraction") or 0) > 1.5 else 1.0
    out = {"date": info.get("DateOfReport") or instants.get("MainI"), "symbol": info.get("Symbol"), "company": info.get("NameOfTheCompany"),
           "total_shares": total, "holders": cats["total"].get("holders"), "flags": flags, "categories": {}}
    for key, row in cats.items():
        pct = (row["fraction"] * 100 * scale if row.get("fraction") is not None
               else (row["shares"] / total * 100 if row.get("shares") is not None and total else None))
        out["categories"][key] = {"pct": pct, "shares": row.get("shares"), "holders": row.get("holders")}
    promoters, named_ok, old_rows = promoter_holders(holder_facts, scale, promoter_pct=out["categories"]["promoter"]["pct"])
    out["promoter_holders"] = promoters
    # False when the filing (older format) does not say which named holders are promoters: an entity missing from it is
    # "not named in that filing", not one that joined or left the promoter group.
    out["promoters_named"] = named_ok
    out["large_public_holders"] = large_public_holders(holder_facts, scale, skip=old_rows)
    out["beneficial_owners"] = beneficial_owners(sbo_facts)
    out["notes"] = " ".join(notes.split())[:4000] if notes else None
    c = out["categories"]
    if "dii" not in c and "institutions_old" in c:
        # Pre-2022 format: one Institutions block with FPIs inside it.
        fpi = sum((c.get(k) or {}).get("pct") or 0 for k in ("fpi_1", "fpi_2"))
        c["dii"] = {"pct": (c["institutions_old"]["pct"] or 0) - fpi, "shares": None, "holders": None, "derived": True}
        c.setdefault("fii", {"pct": fpi, "shares": None, "holders": None, "derived": True})
    if "fpi_1" in c or "fpi_2" in c:
        c["fpi"] = {"pct": sum((c.get(k) or {}).get("pct") or 0 for k in ("fpi_1", "fpi_2")), "shares": None, "holders": None}
    promoter = cats["promoter"]
    shares = promoter.get("shares")
    pledged, encumbered = promoter.get("pledged"), promoter.get("encumbered")
    if pledged is None and flags.get("pledged") is False:
        pledged = 0.0
    if encumbered is None and not any(flags.get(k) for k in ("pledged", "non_disposal", "other")) and flags:
        encumbered = 0.0
    other = None if encumbered is None or pledged is None else max(encumbered - pledged, 0.0)
    out["pledge"] = {"pledged_shares": pledged, "encumbered_shares": encumbered, "other_encumbered_shares": other,
                     "pledged_pct_of_promoter": pledged / shares * 100 if pledged is not None and shares else None,
                     "pledged_pct_of_total": pledged / total * 100 if pledged is not None and total else None,
                     "other_encumbered_pct_of_promoter": other / shares * 100 if other is not None and shares else None,
                     "encumbered_pct_of_promoter": encumbered / shares * 100 if encumbered is not None and shares else None}
    return out


# Named holders sit in typed-dimension contexts: the name in a duration context, the holding in an instant context
# with the same axis and domain value (e.g. DetailsOfSharesHeldByInsuranceCompaniesAxis / InsuranceCompanies_Context15).
HOLDER_FACTS = {"NameOfTheShareholder": "name", "ShareholdingAsAPercentageOfTotalNumberOfShares": "fraction", "NumberOfShares": "shares",
                "TypeOfPromoterShareholding": "promoter_type", "WhetherACategoryOrMoreThan1PercentageOfShareholding": "row_kind"}
HOLDER_KINDS = (("MutualFunds", "mutual fund"), ("Insurance", "insurer"), ("ForeignPortfolioInvestor", "foreign portfolio investor"),
                ("ForeignDirectInvestment", "foreign direct investor"), ("Banks", "bank"), ("ProvidentFunds", "provident/pension fund"),
                ("AlternativeInvestmentFunds", "alternative investment fund"), ("SovereignWealth", "sovereign wealth fund"),
                ("BodiesCorporate", "company"), ("Government", "government"), ("Individual", "individual"))
LARGE_HOLDER_PCT = 1.0  # SEBI requires public holders above 1% to be named in the shareholding pattern


def named_contexts(root):
    """contextRef -> (axis, domain value) for typed-dimension contexts (one named shareholder each)."""
    out = {}
    for ctx in root.iter():
        if local(ctx.tag) != "context":
            continue
        typed = [e for e in ctx.iter() if local(e.tag) == "typedMember"]
        if len(typed) == 1:
            value = "".join(typed[0].itertext()).strip()
            out[ctx.attrib.get("id")] = (typed[0].attrib.get("dimension", "").rsplit(":", 1)[-1], value)
    return out


def _category_row(f):
    name = " ".join((f.get("name") or "").split())
    return (f.get("row_kind") or "").lower() == "category" or (bool(f.get("category_name")) and name.lower() == f["category_name"].strip().lower())


def _holder_pct(f, scale):
    try:
        return float(f["fraction"]) * 100 * scale
    except (KeyError, ValueError, TypeError):
        return None


def large_public_holders(facts, scale=1.0, skip=()):
    """Public (non-promoter) shareholders named with more than 1% of the company, largest first. `skip` holds the
    (axis, id) keys an older-format filing uses for promoter entities."""
    rows = []
    for key, f in facts.items():
        axis = key[0]
        name = " ".join((f.get("name") or "").split())
        if not name or f.get("promoter_type") or key in skip or _category_row(f):
            continue
        pct = _holder_pct(f, scale)
        if pct is not None and pct > LARGE_HOLDER_PCT - 1e-9:
            kind = next((k for k_axis, k in HOLDER_KINDS if k_axis in axis), "other")
            if kind == "other" and GOVERNMENT_NAME.search(name):
                kind = "government"
            rows.append({"name": name, "pct": pct, "kind": kind})
    return sorted(rows, key=lambda r: -r["pct"])


# Before the 2025 format added a promoter flag to each named holder, promoter entities sat on these axes (the promoter
# table's individuals/HUF and "any other" rows, Indian and foreign); public "any other" rows use OtherNonInstitutions.
OLD_PROMOTER_AXES = ("DetailsSharesHeldByIndividualsOrHUFAxis", "DetailsOfSharesHeldByOthersIndianShareholdersAxis",
                     "DetailsOfSharesHeldByOtherForeignShareholdersAxis")
OLD_PROMOTER_TOLERANCE = 0.5  # points: the named rows must add up to the promoter category to be taken as its entities


def promoter_holders(facts, scale=1.0, promoter_pct=None):
    """Named promoter entities with a holding, largest first (the parent company of a listed subsidiary shows here:
    Reliance Industries holds 64.48% of Jio Platforms). Returns (rows, named, old_rows): `named` is False when an
    older-format filing does not mark promoter rows and its promoter-table rows do not add up to the promoter category;
    `old_rows` are the keys taken as promoter rows in an older-format filing (left out of the public holders)."""
    def row(f):
        name = " ".join((f.get("name") or "").split())
        pct = _holder_pct(f, scale)
        return {"name": name, "pct": pct, "shares": _float(f.get("shares"))} if name and pct is not None and pct > 0 else None
    flagged = any(f.get("promoter_type") for f in facts.values())
    if flagged:
        rows = [r for f in facts.values() if f.get("promoter_type") and not _category_row(f) for r in [row(f)] if r]
        return sorted(rows, key=lambda r: -r["pct"]), True, set()
    keys = {k for k, f in facts.items() if k[0] in OLD_PROMOTER_AXES and not _category_row(f)}
    rows = [r for k in keys for r in [row(facts[k])] if r]
    if promoter_pct is not None and not rows and promoter_pct < 0.005:
        return [], True, keys  # no promoter group, nothing to name
    if rows and promoter_pct is not None and abs(sum(r["pct"] for r in rows) - promoter_pct) <= OLD_PROMOTER_TOLERANCE:
        return sorted(rows, key=lambda r: -r["pct"]), True, keys
    return [], False, keys


GOVERNMENT_NAME = re.compile(r"\bPresident of India\b|\bGovernor of\b|\bGovernment of India\b|Department of Investment and Public Asset|"
                             r"\bDIPAM\b|Specified Undertaking of (?:the )?Unit Trust|\bSUUTI\b", re.I)
SBO_FACTS = {"NameOfSignificantBeneficialOwners": "owner", "NameOfRegisteredOwner": "registered_owner",
             "NationalityOfSignificantBeneficialOwners": "nationality",
             "DetailsOfHoldingExerciseOfRightOfTheSBOInTheReportingCompanyWhetherByVirtueOfShares": "fraction"}


def beneficial_owners(facts):
    """Significant beneficial owners the filing names, by registered owner: {'Bharti Telecom Limited': ['Sunil Bharti
    Mittal', ...]} (SEBI asks the pattern to show who ultimately owns a holding vehicle of 10% or more)."""
    out = {}
    for f in facts.values():
        owner, holder = " ".join((f.get("owner") or "").split()), " ".join((f.get("registered_owner") or "").split())
        if owner and holder and owner.upper() != holder.upper():
            names = out.setdefault(holder, [])
            if owner not in names:
                names.append(owner)
    return out


def pledge_disclosure(raw):
    """NSE's promoter-pledge disclosure: % of promoter holding and of all shares pledged, as of a shareholding date."""
    data = json.loads(raw)
    rows = data.get("data") if isinstance(data, dict) else None
    if not rows:
        return None
    r = rows[0]
    as_of, updated = nse_day(r.get("shp")), nse_day(r.get("broadcastDt"))
    return {"as_of": as_of.isoformat() if as_of else None, "updated": updated.isoformat() if updated else None,
            "promoter_pct": _float(r.get("percPromoterHolding")), "pledged_pct_of_promoter": _float(r.get("percPromoterShares")),
            "pledged_pct_of_total": _float(r.get("percTotShares")), "pledged_shares": _float(r.get("totPromoterShares")),
            "depository_pledged_pct_all_holders": _float(r.get("percSharesPledged")),
            "basis": "NSE promoter-pledge disclosure (SEBI SAST Regulation 31)"}


ROWS = (("promoter", "Promoters & promoter group", 0), ("promoter_government", "of which Central / State Government (President of India)", 1),
        ("fii", "Foreign institutions (FII/FPI)", 0), ("fpi", "of which FPIs", 1),
        ("fdi", "of which foreign direct investment", 1), ("dii", "Domestic institutions (DII)", 0), ("mf", "of which mutual funds", 1),
        ("insurance", "of which insurance companies", 1), ("government", "Government (public category, apart from DII)", 0),
        ("central_government", "of which Central Government / President of India", 1),
        ("state_governments", "of which state governments / governors", 1),
        ("government_companies", "of which companies where a government is promoter", 1),
        ("non_institutions", "Retail and others (non-institutions)", 0), ("small_individuals", "of which individuals up to ₹2 lakh", 1),
        ("employee_trusts", "Employee trusts (non-promoter, non-public)", 0), ("public", "Total public shareholding", 0))


GOVERNMENT_PARTS = ("central_government", "state_governments", "government_companies")


def shareholding(raw_symbol, *, fetcher=nse_request, clock=instant, trend=8, older=2):
    """Latest shareholding pattern with changes against the previous readable quarter.

    When the previous quarter's XBRL cannot be read, up to `older` earlier quarters are tried and the comparison is
    labelled with the quarter actually used. A new listing with no quarter-end pattern yet falls back to the latest
    pattern NSE lists (filed on listing or for a share issue), labelled as such."""
    symbol = str(raw_symbol).strip().upper()
    if symbol.endswith(".NS"):
        symbol = symbol[:-3]
    if not re.fullmatch(r"[A-Z0-9&_-]{1,30}", symbol):
        raise Block("SHAREHOLDING: use an NSE symbol such as ADANIPORTS")
    now = clock()
    try:
        raw = fetcher(api_url("/api/corporate-share-holdings-master", symbol))
        listed = filings(raw)
        kind = "quarterly"
        if not listed:
            listed, kind = filings(raw, quarterly=False)[:1], "adhoc"
    except (ValueError, TypeError, KeyError):
        raise Block("SHAREHOLDING: NSE's filing list was not readable; try again later") from None
    if not listed:
        raise Block(f"SHAREHOLDING: NSE lists no quarterly shareholding filings for {symbol}, and no pattern filed on listing either; "
                    "check the symbol (NSE only, not BSE codes). A company listed in the last few weeks may not have filed one yet.")
    errors, parsed = {}, {}
    latest_entry = listed[0]

    def read(entry):
        if not entry["xbrl"]:
            return None
        try:
            return parse_xbrl(fetcher(entry["xbrl"]))
        except (Block, ValueError, KeyError, TypeError, ET.ParseError) as error:
            errors[entry["quarter"]] = f"XBRL for {entry['quarter']} unreadable ({error})"
            return None
    latest = read(latest_entry)
    previous, compared = None, None
    if latest:
        # The previous quarter, or the next-older one whose filing can be read (labelled with the quarter used).
        for entry in listed[1:2 + older]:
            previous = read(entry)
            if previous:
                compared = entry
                break
    pledge = None
    try:
        pledge = pledge_disclosure(fetcher(api_url("/api/corporate-pledgedata", symbol)))
    except (Block, ValueError, KeyError, TypeError):
        errors["pledge_disclosure"] = "NSE's promoter-pledge disclosure was unavailable"
    rows = []
    if latest:
        for key, label, level in ROWS:
            now_row = latest["categories"].get(key)
            if not now_row or now_row.get("pct") is None:
                continue
            before = ((previous or {}).get("categories") or {}).get(key) or {}
            if (level or key in ("government", "employee_trusts")) and not now_row["pct"] and not before.get("pct"):
                continue  # an empty line is noise
            if key == "fpi" and abs(now_row["pct"] - ((latest["categories"].get("fii") or {}).get("pct") or 0)) < 0.005:
                continue  # all foreign institutional holding is FPI; the line would repeat the one above
            if key in GOVERNMENT_PARTS and abs(now_row["pct"] - ((latest["categories"].get("government") or {}).get("pct") or 0)) < 0.005:
                continue  # one kind of government holding makes up the whole government line
            change = now_row["pct"] - before["pct"] if before.get("pct") is not None else None
            rows.append({"key": key, "label": label, "level": level, "pct": now_row["pct"], "previous_pct": before.get("pct"), "change_pts": change})
    else:
        # Fall back to the totals NSE prints in its filing list.
        prev = listed[1] if len(listed) > 1 else {}
        for key, label, field in (("promoter", "Promoters & promoter group", "promoter_pct"), ("public", "Total public shareholding", "public_pct")):
            if latest_entry.get(field) is not None:
                change = latest_entry[field] - prev[field] if prev.get(field) is not None else None
                compared = prev if change is not None else compared
                rows.append({"key": key, "label": label, "level": 0, "pct": latest_entry[field], "previous_pct": prev.get(field), "change_pts": change})
    trend_rows = [{"quarter": e["quarter"], "promoter_pct": e["promoter_pct"]} for e in listed[:trend] if e.get("promoter_pct") is not None]
    moves = promoter_moves(trend_rows)
    by_quarter = {e["quarter"]: e for e in listed}
    parsed_by_quarter = {latest_entry["quarter"]: latest, **({compared["quarter"]: previous} if compared else {})}

    def parsed_for(quarter):
        # Quietly: an extra quarter that cannot be read only leaves a gap (no share counts, no pledge figure).
        if quarter not in parsed_by_quarter:
            entry = by_quarter.get(quarter)
            try:
                parsed_by_quarter[quarter] = parse_xbrl(fetcher(entry["xbrl"])) if entry and entry.get("xbrl") else None
            except (Block, ValueError, KeyError, TypeError, ET.ParseError):
                parsed_by_quarter[quarter] = None
        return parsed_by_quarter[quarter]
    # The pledge and encumbrance trend: every quarter of the promoter trend, its filings read in parallel.
    if latest and kind == "quarterly":
        unread = [e["quarter"] for e in listed[:trend] if e["quarter"] not in parsed_by_quarter]
        if unread:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(parsed_for, unread))
    pledge_rows = pledge_trend([(e["quarter"], parsed_by_quarter.get(e["quarter"])) for e in listed[:trend]])
    sast, events = None, []
    sast_raw = None
    encumbered = any(((parsed_by_quarter.get(e["quarter"]) or {}).get("pledge") or {}).get("encumbered_shares") for e in listed[:trend])
    if latest and kind == "quarterly" and (moves or encumbered):
        try:
            sast_raw = fetcher(api_url("/api/corporate-sast-reg29", symbol))
        except (Block, ValueError, KeyError, TypeError):
            if encumbered:  # a promoter move alone already points the reader to the SAST filings
                errors["sast"] = "NSE's SAST disclosure list was unavailable, so lenders' encumbrance filings are not summarised"
    if sast_raw is not None:
        try:
            sast = sast_filings(sast_raw)
        except (ValueError, KeyError, TypeError):
            sast = None
        try:
            start = quarter_start(date.fromisoformat(listed[min(trend, len(listed)) - 1]["quarter"])).isoformat()
            events = [e for e in encumbrance_events(sast_raw) if e["date"] >= start]
        except (ValueError, KeyError, TypeError):
            events = []
    if moves:
        for m in moves:
            m["filings"] = None if sast is None else filings_in_window(sast, m["quarter"])
            before, after = parsed_for(m["from_quarter"]), parsed_for(m["quarter"])
            if before and after:
                m["shares_before"], m["shares_after"] = before.get("total_shares"), after.get("total_shares")
                m["holder_changes"] = promoter_changes(before, after)
                unnamed = [q for q, x in ((m["from_quarter"], before), (m["quarter"], after)) if not x.get("promoters_named", True)]
                if unnamed:
                    m["unnamed_filings"] = unnamed
            if m["filings"] == []:
                names = [h["name"] for x in (before, after) if x for h in x.get("promoter_holders") or []]
                m["deals"] = promoter_deals(fetcher, symbol, m["quarter"], names)
    age = (now.date() - date.fromisoformat(latest_entry["quarter"])).days
    large, dropped = match_large_holders((latest or {}).get("large_public_holders") or [], (previous or {}).get("large_public_holders") or [])
    previous_quarter = listed[1]["quarter"] if len(listed) > 1 else None
    compared_quarter = compared["quarter"] if compared else None
    due = next_quarter_due(latest_entry["quarter"]) if kind == "adhoc" else (None, None)
    return {"schema": "shareholding_v1", "symbol": symbol, "name": latest_entry.get("name") or (latest or {}).get("company") or symbol,
            "as_of": now.isoformat(), "quarter": latest_entry["quarter"], "filed": latest_entry["filed"], "revised": latest_entry["revised"],
            "filing_kind": kind, "next_quarter_end": due[0], "next_filing_due": due[1],
            "previous_quarter": previous_quarter, "compared_quarter": compared_quarter,
            # True when the holder-level comparison (named holders, categories) really read an earlier filing.
            "previous_read": bool(latest and previous), "xbrl": latest_entry["xbrl"],
            "rows": rows, "holders": (latest or {}).get("holders"), "previous_holders": (previous or {}).get("holders"),
            "total_shares": (latest or {}).get("total_shares"),
            "pledge": (latest or {}).get("pledge"), "encumbrance_flags": (latest or {}).get("flags"), "pledge_disclosure": pledge,
            "promoter_holders": (latest or {}).get("promoter_holders") or [],
            "promoter_notes": promoter_notes((latest or {}).get("promoter_holders") or [], latest or {}),
            "large_holders_dropped": dropped,
            "promoter_trend": trend_rows, "promoter_trend_shape": trend_shape(trend_rows), "promoter_moves": moves,
            "pledge_trend": pledge_rows, "encumbrance_events": events,
            "no_promoter": bool(latest) and not (latest.get("categories", {}).get("promoter") or {}).get("shares") and not trend_rows_have_promoter(trend_rows),
            "large_public_holders": large, "stale": age > 140, "errors": errors, "source": SOURCE_NOTE,
            "note": ("Shareholding is filed within 21 days of each quarter end, so it can be up to about four months old. "
                     "Foreign institutions follow the exchange's 'Institutions (Foreign)' category (FPIs plus any foreign direct "
                     "investment); retail and others are the 'Non-institutions' category."),
            "execution_eligible": False, "submitted": False}


# Fund houses, matched at the start of a named holder: each quarter's filing names a mutual fund or LIC by a different
# scheme ("LICI NEW PENSION PLUS SECURED FUND" in June, "LICI INDEX PLUS FLEXI SMART GROWTH FUND" in September, both
# LIC's whole holding), so holders are compared house by house, not by the scheme's name.
FUND_HOUSES = (
    ("LIC", r"LICI\b|LIC\b|LIFE INSURANCE CORPORATION OF INDIA"),
    ("SBI", r"SBI\b"), ("ICICI Prudential", r"ICICI PRUDENTIAL\b"), ("HDFC", r"HDFC\b"),
    ("Nippon India", r"NIPPON\b|RELIANCE CAPITAL TRUSTEE"), ("Kotak", r"KOTAK\b"), ("Axis", r"AXIS\b"), ("UTI", r"UTI\b"),
    ("Mirae Asset", r"MIRAE\b"), ("Aditya Birla Sun Life", r"ADITYA BIRLA SUN LIFE|ABSL\b"), ("DSP", r"DSP\b"),
    ("Franklin Templeton", r"FRANKLIN\b|TEMPLETON\b"), ("Tata", r"TATA\b"), ("Parag Parikh", r"PARAG PARIKH|PPFAS\b"),
    ("Motilal Oswal", r"MOTILAL OSWAL"), ("Canara Robeco", r"CANARA ROBECO"), ("Invesco", r"INVESCO\b"), ("HSBC", r"HSBC\b"),
    ("Bandhan", r"BANDHAN\b|IDFC\b"), ("Edelweiss", r"EDELWEISS\b"), ("Quant", r"QUANT\b"), ("PGIM India", r"PGIM\b"),
    ("Sundaram", r"SUNDARAM\b"), ("Mahindra Manulife", r"MAHINDRA MANULIFE"), ("Baroda BNP Paribas", r"BARODA BNP"),
    ("WhiteOak Capital", r"WHITEOAK\b"), ("360 ONE", r"360 ONE\b"), ("Union", r"UNION\b"), ("Bajaj Finserv", r"BAJAJ FINSERV\b"),
)
HOUSE_KINDS = {"mutual fund", "insurer", "provident/pension fund"}
_HOUSES = [(label, re.compile(r"(?:THE\s+)?(?:" + pattern + r")")) for label, pattern in FUND_HOUSES]


def fund_house(name, kind):
    """(house label, label to show) for a mutual fund, insurer or pension-fund holder named by one of its schemes, else
    None: ('LIC', 'LIC (all schemes)') for 'LICI INDEX PLUS FLEXI SMART GROWTH FUND'; ('SBI', 'SBI Mutual Fund (all
    schemes)') for 'SBI LARGE CAP FUND'; NPS Trust accounts by their pension-fund manager."""
    if kind not in HOUSE_KINDS:
        return None
    text = " ".join(str(name or "").upper().replace("-", " - ").split())
    if text.startswith("NPS TRUST"):
        rest = re.sub(r"^NPS TRUST\s*(?:-\s*)?(?:A\s*/?\s*C\.?|AC)?\s*(?:-\s*)?", "", text)
        sub = next((label for label, rx in _HOUSES if rx.match(rest)), None)
        return ("NPS:" + (sub or rest[:20]), f"NPS Trust ({sub} pension fund schemes)" if sub else None)
    for label, rx in _HOUSES:
        if rx.match(text):
            if label == "LIC" and kind == "insurer":
                return "LIC", "LIC (all schemes)"
            if kind == "mutual fund":
                return label, f"{label} Mutual Fund (all schemes)"
            return label, None  # an insurer or pension fund other than LIC keeps its filed name
    return None


def _house_rows(rows):
    """Named holders grouped by fund house (a house named twice in one filing is added up); others kept one by one."""
    out, by_house = [], {}
    for h in rows:
        found = fund_house(h["name"], h.get("kind"))
        if not found:
            out.append({**h, "house": None, "filed_as": [h["name"]]})
            continue
        key = (found[0], h.get("kind"))
        if key in by_house:
            g = by_house[key]
            g["pct"] += h["pct"]
            g["filed_as"].append(h["name"])
            g["name"] = found[1] or g["name"]
        else:
            g = {**h, "house": found[0], "filed_as": [h["name"]]}
            # The plain house name ('Life Insurance Corporation of India', 'SBI Mutual Fund') stays as filed.
            plain = re.fullmatch(r"(?:THE\s+)?(?:LIFE INSURANCE CORPORATION OF INDIA|[A-Z0-9 &]+ MUTUAL FUND)", " ".join(h["name"].upper().split()))
            g["name"] = h["name"] if plain or not found[1] else found[1]
            by_house[key] = g
            out.append(g)
    return sorted(out, key=lambda r: -r["pct"])


def match_large_holders(latest, previous):
    """Each public holder above 1% with its previous-quarter holding (None when it was not named then), matched by fund
    house for mutual funds, LIC and NPS accounts, and by entity name otherwise ('Government of Singapore - E' is
    'Government of Singapore'). Returns (rows, dropped): `dropped` are those named last quarter but not now."""
    now, before = _house_rows(latest), _house_rows(previous)
    used = set()
    out = []
    for h in now:
        prior = None
        for i, p in enumerate(before):
            if i in used:
                continue
            same = ((h["house"], h.get("kind")) == (p["house"], p.get("kind")) if h["house"] or p["house"]
                    else h["name"].upper() == p["name"].upper() or same_entity(h["name"], p["name"]))
            if same:
                prior = i
                break
        if prior is not None:
            used.add(prior)
        out.append({**h, "previous_pct": before[prior]["pct"] if prior is not None else None,
                    "previous_filed_as": before[prior]["filed_as"] if prior is not None else None})
    dropped = [{"name": p["name"], "pct": p["pct"], "kind": p.get("kind")} for i, p in enumerate(before) if i not in used]
    return out, dropped


# Well-known promoter holding vehicles, from a curated list (assets/business_peers.json, "promoter_vehicles").
def load_vehicles(path=None):
    try:
        data = json.loads(Path(path or Path(__file__).resolve().parents[1] / "assets" / "business_peers.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    vehicles = data.get("promoter_vehicles") if isinstance(data, dict) else None
    return {k: v for k, v in (vehicles or {}).items() if not k.startswith("_") and isinstance(v, dict)}


NOTE_SENTENCE = 180  # characters: a longer note sentence is legal boilerplate, not a description


def promoter_notes(holders, parsed, vehicles=None):
    """{entity name: short note} for promoter entities, only from the filing (its notes, its significant beneficial
    owners) or the curated list of well-known holding vehicles; nothing is inferred from the name."""
    vehicles = load_vehicles() if vehicles is None else vehicles
    sentences = [s.strip().rstrip(".") for part in re.split(r"(?:^|\s)\d{1,2}\.\s+", parsed.get("notes") or "")
                 for s in re.split(r"(?<=[a-z\)])\.\s+(?=[A-Z])", part) if s.strip()]
    owners = parsed.get("beneficial_owners") or {}
    out = {}
    for h in holders:
        bits = []
        curated = next((v for k, v in vehicles.items() if name_key(k) == name_key(h["name"])), None)
        if curated and curated.get("note"):
            bits.append(curated["note"] + (f" [curated list, checked {curated['checked']}]" if curated.get("checked") else " [curated list]"))
        width = len(h["name"].split())
        said = next((s for s in sentences if len(s) <= NOTE_SENTENCE and name_key(" ".join(s.split()[:width])) == name_key(h["name"])
                     and re.search(r"promoter|holding company|subsidiary|acting in concert|\bPAC\b|trust", s, re.I)), None)
        if said:
            bits.append(f'filing note: "{said}"')
        sbo = next((v for k, v in owners.items() if name_key(k) == name_key(h["name"])), None)
        if sbo:
            bits.append("significant beneficial owners named in the filing: " + ", ".join(holder_name(n) for n in sbo[:4])
                        + (f" and {len(sbo) - 4} others" if len(sbo) > 4 else ""))
        if bits:
            out[h["name"]] = "; ".join(bits)
    return out


def trend_rows_have_promoter(rows):
    return any((x.get("promoter_pct") or 0) > 0 for x in rows or [])


def pledge_trend(pairs):
    """[(quarter, parsed filing or None)] newest first -> [{'quarter', 'pledged_pct', 'encumbered_pct'}] for the quarters
    whose filing was read (percentages of the promoter holding; encumbered includes pledges and non-disposal undertakings)."""
    rows = []
    for quarter, parsed in pairs:
        p = (parsed or {}).get("pledge") or {}
        if p.get("pledged_pct_of_promoter") is None and p.get("encumbered_pct_of_promoter") is None:
            continue
        rows.append({"quarter": quarter, "pledged_pct": p.get("pledged_pct_of_promoter"), "encumbered_pct": p.get("encumbered_pct_of_promoter")})
    return rows


def pledge_trend_line(r):
    """'Promoter pledge over Dec 2024–Sep 2026 (8 quarterly filings): none in any quarter, and nothing otherwise encumbered.'"""
    rows = r.get("pledge_trend") or []
    if len(rows) < 2:
        return None
    old = list(reversed(rows))
    span = f"{mon(old[0]['quarter'])}–{mon(old[-1]['quarter'])} ({len(rows)} quarterly filings)"
    # A quarter with no separate pledge figure but nothing encumbered at all had no pledge either (a pledge is an encumbrance).
    old = [{**x, "pledged_pct": 0.0} if x["pledged_pct"] is None and x["encumbered_pct"] is not None and abs(x["encumbered_pct"]) < 0.005 else x
           for x in old]
    pledged = [x for x in old if x["pledged_pct"] is not None]
    encumbered = [x for x in old if x["encumbered_pct"] is not None]
    missing = [x["quarter"] for x in old if x["pledged_pct"] is None]
    zero = lambda xs, k: bool(xs) and all(abs(x[k]) < 0.005 for x in xs)
    # Filings before mid-2025 give pledges and other encumbrance as one figure: no separate pledge figure is "no data".
    gap = (f" (no separate pledge figure for {', '.join(mon(q) for q in missing)}: the older filing format gives pledges and other "
           "encumbrance as one figure)") if missing and pledged else ""
    if not pledged:
        text = f"Promoter encumbrance over {span}"
    elif zero(pledged, "pledged_pct"):
        text = (f"Promoter pledge over {span}: none in any quarter" if not missing else
                f"Promoter pledge over {span}: none in the {len(pledged)} quarters that report it{gap}")
        if zero(encumbered, "encumbered_pct"):
            return text + ", and nothing otherwise encumbered."
    else:
        peak = max(pledged, key=lambda x: (x["pledged_pct"], x["quarter"]))
        text = (f"Pledged share of the promoter holding over {span}: {pledged[0]['pledged_pct']:.2f}% ({mon(pledged[0]['quarter'])}) → "
                f"{pledged[-1]['pledged_pct']:.2f}% ({mon(pledged[-1]['quarter'])}), peak {peak['pledged_pct']:.2f}% ({mon(peak['quarter'])}){gap}")
    if encumbered and not zero(encumbered, "encumbered_pct"):
        peak = max(encumbered, key=lambda x: (x["encumbered_pct"], x["quarter"]))
        text += (f"{': ' if not pledged else '; '}all encumbrance (pledges, non-disposal undertakings and other) "
                 f"{encumbered[0]['encumbered_pct']:.2f}% → {encumbered[-1]['encumbered_pct']:.2f}%, peak {peak['encumbered_pct']:.2f}% ({mon(peak['quarter'])})")
    return text + "."


BIG_MOVE_PTS = 1.0  # a promoter holding change larger than this in one quarter gets a pointer to the filings behind it
SAST_THRESHOLD_PTS = 2.0  # SAST Reg. 29(2): a 5%+ holder group discloses each change of 2% or more of the shares
BSE_DEALS = ("https://www.bseindia.com/markets/equity/EQReports/tra_Bulk_Deals.aspx",
             "https://www.bseindia.com/markets/equity/EQReports/tra_Block_Deals.aspx")
SAST_GRACE_DAYS = 10  # a SAST disclosure is due within two working days, so one filed just after the quarter still counts


def promoter_moves(rows, limit=3):
    """Quarters (newest first, at most `limit`) in which the promoter holding moved more than BIG_MOVE_PTS: Adani Ports
    +2.13 pts in the December 2025 quarter and -1.99 pts in the June 2026 quarter."""
    old = list(reversed(rows))
    out = [{"quarter": b["quarter"], "from_quarter": a["quarter"], "pts": b["promoter_pct"] - a["promoter_pct"]}
           for a, b in zip(old, old[1:]) if abs(b["promoter_pct"] - a["promoter_pct"]) > BIG_MOVE_PTS]
    return list(reversed(out))[:limit]


def sast_filings(raw):
    """NSE's SAST Regulation 29 disclosures (acquisitions and sales crossing 5% or moving 2% by holders and promoters):
    [{'acquirer', 'promoter', 'type', 'pct', 'mode', 'filed', 'period_end', 'url'}]."""
    data = json.loads(raw)
    rows = data.get("data") if isinstance(data, dict) else data
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("acqSaleType") or "").strip()
        sale = kind.lower() in ("sale", "disposal")
        moved = _float(str(r.get("totSaleShare") if sale else r.get("totAcqShare") or ""))
        try:
            filed = datetime.strptime(str(r.get("timestamp") or r.get("time") or "")[:11].title(), "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        end = re.search(r"to\s+(\d{2}-[A-Za-z]{3}-\d{4})", str(r.get("acquirerDate") or ""))
        try:
            period_end = datetime.strptime(end.group(1).title(), "%d-%b-%Y").date().isoformat() if end else None
        except ValueError:
            period_end = None
        out.append({"acquirer": " ".join(str(r.get("acquirerName") or "").split()), "promoter": r.get("promoterType") == "Y",
                    "type": "sale" if sale else "acquisition" if kind.lower() == "acquisition" else kind.lower() or "change",
                    "pct": moved, "mode": " ".join(str(r.get("acquisitionMode") or "").split()) or None, "filed": filed,
                    "period_end": period_end, "url": r.get("attachement")})
    return out


ENCUMBRANCE_HOLDER = re.compile(r"trustee|security agent|\bagent\b|agency|\bbank\b|debenture|lender|trust (?:company|corporation)|"
                                r"security trust|bondholders|noteholders", re.I)


def encumbrance_events(raw):
    """SAST disclosures (NSE's Reg. 29 list) by lenders and their security trustees on a company's shares: encumbrance
    created, released or invoked. NSE exposes no separate Reg. 31 (promoter encumbrance) list, but each encumbrance holder
    discloses here when it takes or gives up security over 5% or more (Vedanta, 2026: GLAS Agency, Citicorp, Axis Trustee
    and Kroll as trustees for the promoter's lenders, each over 51.93-54.72% of the company). Returns
    [{'date', 'holder', 'action', 'pct', 'filed', 'mode'}], newest first."""
    data = json.loads(raw)
    rows = data.get("data") if isinstance(data, dict) else data
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or r.get("promoterType") == "Y":
            continue
        holder = " ".join(str(r.get("acquirerName") or "").split())
        kind, mode = str(r.get("acqType") or ""), " ".join(str(r.get("acquisitionMode") or "").split())
        if "encumbrance" not in (kind + " " + mode).lower() and not ENCUMBRANCE_HOLDER.search(holder):
            continue
        end = re.search(r"to\s+(\d{2}-[A-Za-z]{3}-\d{4})", str(r.get("acquirerDate") or ""))
        day = nse_day(end.group(1)) if end else None
        filed = nse_day(str(r.get("timestamp") or r.get("time") or "")[:11])
        if not day and not filed:
            continue
        sale = str(r.get("acqSaleType") or "").strip().lower() in ("sale", "disposal")
        action = ("invoked" if "invoc" in mode.lower() else "released" if sale or "release" in mode.lower() else "created")
        after, moved = _float(r.get("totAftShare")), _float(r.get("totSaleShare") if sale else r.get("totAcqShare"))
        pct = moved if action == "released" else (after if after else moved)
        out.append({"date": (day or filed).isoformat(), "filed": filed.isoformat() if filed else None, "holder": holder,
                    "action": action, "pct": pct, "mode": mode or None})
    return sorted(out, key=lambda e: e["date"], reverse=True)


def short_holder(name):
    """'Citicorp International Limited' from 'Citicorp International Limited (as trustee for the holders of the Bonds)'."""
    head = re.split(r"\s*\((?:as|in its|formerly|acting|on behalf)\b|\s+(?:acting|as agent|as trustee|on behalf)\b|,", name, maxsplit=1, flags=re.I)[0]
    return head.strip() or name


def encumbrance_line(r, limit=4):
    """'Lender and trustee SAST filings on the shares over Dec 2024–Sep 2026: ...' or None."""
    events = r.get("encumbrance_events") or []
    if not events:
        return None
    rows = r.get("pledge_trend") or r.get("promoter_trend") or []
    span = f"{mon(rows[-1]['quarter'])}–{mon(rows[0]['quarter'])}" if rows else "the period shown"
    counts = {a: sum(e["action"] == a for e in events) for a in ("created", "released", "invoked")}
    words = {"created": "took security over", "released": "released security over", "invoked": "invoked security over"}
    shown = "; ".join(f"{month(e['date'])} {short_holder(e['holder'])} {words[e['action']]}"
                      + (f" {e['pct']:.2f}% of the shares" if e.get("pct") else " (share not stated)") for e in events[:limit])
    tally = ", ".join(f"{n} {a}" for a, n in counts.items() if n)
    return (f"Encumbrance filings by lenders and their trustees over {span} (SAST disclosures on NSE; {tally}): {shown}"
            + (f"; {len(events) - limit} more" if len(events) > limit else "")
            + ". A trustee taking security over a promoter's shares is the lenders' side of a pledge or non-disposal undertaking.")


def filings_in_window(sast, quarter_end):
    """Promoter-group SAST disclosures for the quarter ending `quarter_end`: transaction period ending inside the quarter,
    or (when that is missing) filed inside it or within SAST_GRACE_DAYS after it."""
    end = date.fromisoformat(quarter_end)
    start = quarter_start(end)
    grace = end + timedelta(days=SAST_GRACE_DAYS)
    hits = []
    for f in sast:
        if not f["promoter"]:
            continue
        day = date.fromisoformat(f["period_end"]) if f["period_end"] else None
        filed = date.fromisoformat(f["filed"])
        if (day and start <= day <= end) or (not day and start <= filed <= grace):
            hits.append(f)
    return sorted(hits, key=lambda f: (-(f["pct"] or 0), f["acquirer"]))


NAME_FILLER = {"LIMITED", "LTD", "PVT", "PRIVATE", "PTE", "INC", "LLC", "LLP", "THE", "AND", "CO", "COMPANY", "OF", "ALONGWITH", "PAC", "PACS"}


def name_key(name):
    """Comparable words of an entity name: 'Carmichael Rail and Port Singapore HOlding Pvt Ltd' and 'Carmichael Rail And
    Port Singapore Holdings Pte. Ltd.' give the same key."""
    words = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper().replace("&", " AND ")).split()
    return tuple(w[:-1] if len(w) > 3 and w.endswith("S") else w for w in words if w not in NAME_FILLER)


def same_entity(a, b):
    """True when two entity names are the same holder: equal keys, or one key (two words or more) starting the other."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    short, long_ = sorted((ka, kb), key=len)
    return ka == kb or (len(short) >= 2 and long_[:len(short)] == short)


def promoter_changes(before, after, min_pts=0.25):
    """Promoter entities whose share count moved by at least `min_pts` of the company between two filings (or that
    joined or left the promoter list): [{'name', 'before_pct', 'after_pct', 'shares_change', 'pts'}], biggest first."""
    def holdings(parsed):
        total = parsed.get("total_shares")
        out = []
        for h in parsed.get("promoter_holders") or []:
            shares = h.get("shares")
            share = shares / total * 100 if shares is not None and total else h.get("pct") if (h.get("pct") or 0) <= 100 else None
            if share is not None:
                out.append({"name": h["name"], "shares": shares, "pct": share})
        return out
    old, new, total = holdings(before), holdings(after), after.get("total_shares")
    # An older-format filing that does not mark its promoter rows names nobody: an entity missing from it was not named
    # in that filing, which says nothing about whether it joined or left the promoter group.
    old_named, new_named = before.get("promoters_named", True), after.get("promoters_named", True)
    if not old_named or not new_named:
        return []
    changes, seen = [], set()
    for h in new:
        prior = next((o for o in old if id(o) not in seen and same_entity(o["name"], h["name"])), None)
        if prior:
            seen.add(id(prior))
        moved = (h["shares"] - prior["shares"] if prior and h["shares"] is not None and prior["shares"] is not None else
                 h["shares"] if not prior else None)
        change_pts = moved / total * 100 if moved is not None and total else h["pct"] - (prior["pct"] if prior else 0)
        if abs(change_pts) >= min_pts:
            changes.append({"name": h["name"], "before_pct": prior["pct"] if prior else None, "after_pct": h["pct"],
                            "shares_change": moved, "pts": change_pts})
    for o in old:
        if id(o) not in seen and o["pct"] >= min_pts:
            changes.append({"name": o["name"], "before_pct": o["pct"], "after_pct": None,
                            "shares_change": -o["shares"] if o["shares"] is not None else None, "pts": -o["pct"]})
    return sorted(changes, key=lambda c: -abs(c["pts"]))


def deal_rows(raw, kind):
    """NSE bulk/block-deal rows: [{'kind', 'date', 'client', 'side', 'qty', 'price'}]."""
    data = json.loads(raw)
    rows = data.get("data") if isinstance(data, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        day = nse_day(r.get("BD_DT_DATE"))
        qty, price = _float(str(r.get("BD_QTY_TRD") or "")), _float(str(r.get("BD_TP_WATP") or ""))
        side = str(r.get("BD_BUY_SELL") or "").strip().lower()
        if day and qty and side in ("buy", "sell"):
            out.append({"kind": kind.split("_")[0], "date": day.isoformat(), "client": " ".join(str(r.get("BD_CLIENT_NAME") or "").split()),
                        "side": side, "qty": qty, "price": price})
    return out


def promoter_deals(fetcher, symbol, quarter_end, promoter_names):
    """NSE bulk and block deals in the quarter ending `quarter_end` by any of `promoter_names`, each with the other side
    of that day's deals at the same price: {'deals': [...], 'checked': [lists read]} ('checked' empty when neither
    list could be read)."""
    end = date.fromisoformat(quarter_end)
    start = quarter_start(end)
    rows, checked = [], []
    for kind in DEAL_KINDS:
        try:
            rows += deal_rows(fetcher(deals_url(kind, symbol, start, end)), kind)
            checked.append(kind)
        except (Block, ValueError, KeyError, TypeError):
            continue
    promoter = lambda client: any(same_entity(client, n) for n in promoter_names)
    mine = [d for d in rows if promoter(d["client"])]
    for d in mine:
        d["other_side"] = [o for o in rows if o["date"] == d["date"] and o["side"] != d["side"] and o["price"] == d["price"]
                           and not promoter(o["client"])]
    return {"deals": sorted(mine, key=lambda d: (d["date"], -d["qty"])), "checked": checked}


def crore_shares(n):
    return f"{n / 1e7:,.2f} cr"


def mode_label(f, m):
    """How a SAST acquisition happened, worded by what the filings show. When the company's share count rose between the
    two quarters by about the stake acquired, the shares were newly issued to the acquirer (Adani Ports, Dec 2025:
    14,38,20,153 new shares to Carmichael Rail and Port Singapore Holding for NQXT, which NSE's SAST row calls 'open
    market'); otherwise NSE's field is quoted as filed, not asserted."""
    before, after = m.get("shares_before"), m.get("shares_after")
    if before and after and after > before * 1.0005 and f.get("type") == "acquisition" and f.get("pct"):
        issued = after - before
        if abs(issued / after * 100 - f["pct"]) <= max(0.1, 0.05 * f["pct"]):
            return (f"allotment of new shares: share count {crore_shares(before)} → {crore_shares(after)}, +{indian(issued)}, "
                    "likely a preferential issue")
    return f"mode as filed: {f['mode'].lower()}" if f.get("mode") else None


def tidy_case(name):
    """'Singapore HOlding' -> 'Singapore Holding': words typed with a stray capital (all-capital words are kept)."""
    return " ".join(w.capitalize() if len(w) > 2 and not w.isupper() and any(c.isupper() for c in w[1:]) else w for w in name.split())


def quarter_start(end):
    """First day of the three-month quarter ending on `end` (30 Jun 2026 -> 1 Apr 2026)."""
    month, year = end.month - 2, end.year
    if month <= 0:
        month, year = month + 12, year - 1
    return date(year, month, 1)


def move_lines(r):
    """One pointer line per large promoter move, naming the SAST filings NSE has for that quarter."""
    out = []
    for m in r.get("promoter_moves") or []:
        start = quarter_start(date.fromisoformat(m["quarter"]))
        window = f"{start.strftime('%b')}–{date.fromisoformat(m['quarter']).strftime('%b %Y')}"
        text = f"Promoter stake {m['pts']:+.2f} pts in the {mon(m['quarter'])} quarter"
        found = m.get("filings")
        if found:
            named, issued = [], False
            for f in found[:3]:
                how = mode_label(f, m)
                issued = issued or "new shares" in (how or "")
                named.append(f"{holder_name(tidy_case(f['acquirer']))} {'sold' if f['type'] == 'sale' else 'acquired' if f['type'] == 'acquisition' else f['type']}"
                             + (f" {f['pct']:.2f}%" if f.get("pct") is not None else "")
                             + (f" ({how}; filed {month(f['filed'])})" if how else f" (filed {month(f['filed'])})"))
            text += (": NSE SAST (Reg. 29) filings by the promoter group in that window: " + "; ".join(named)
                     + (f"; and {len(found) - 3} more" if len(found) > 3 else "")
                     + (f". Check the company's allotment announcement for {window}." if issued else
                        f". Check NSE's preferential-allotment and block-deal filings for {window} too."))
        elif found is not None:
            text += ": no promoter-group SAST (Reg. 29) filing on NSE in that window" + (
                f", and none was required for a net change of this size: SAST Reg. 29(2) asks a promoter group for a disclosure when its "
                f"holding moves by {SAST_THRESHOLD_PTS:.0f}% of the shares or more (counted from its last disclosure, so smaller moves can "
                "add up to one)" if abs(m["pts"]) < SAST_THRESHOLD_PTS else "") + deals_words(m.get("deals"), window)
        else:
            text += f": check NSE's SAST, preferential-allotment and block-deal filings for {window}."
        changed = changes_words(m, skip=[f["acquirer"] for f in found or []])
        if changed:
            text += " " + changed
        out.append(text)
    return out


def deals_words(found, window):
    """What NSE's bulk/block-deal lists show for a promoter move without a SAST filing (ends with a full stop)."""
    if not found or not found.get("checked"):
        return f"; check NSE's SAST, preferential-allotment and block-deal filings for {window}."
    deals = found.get("deals") or []
    lists = " and ".join(k.split("_")[0] for k in found["checked"])
    if not deals:
        return (f"; NSE's {lists}-deal records list no deals by promoter entities in {window}. The trade may have been on BSE (its bulk and "
                f"block deals are at {BSE_DEALS[0]} and {BSE_DEALS[1]}; BSE blocks scripted access, so open them in a browser or search "
                "the web) or off-market, so check those and the company's announcements for that window.")
    parts = []
    for d in deals[:3]:
        other = sorted(d.get("other_side") or [], key=lambda o: -o["qty"])
        to = (f" ({'to' if d['side'] == 'sell' else 'from'} " + ", ".join(f"{holder_name(tidy_case(o['client']))} {crore_shares(o['qty'])}" for o in other[:2])
              + (f" and {len(other) - 2} more" if len(other) > 2 else "") + ")") if other else ""
        parts.append(f"{holder_name(tidy_case(d['client']))} {'sold' if d['side'] == 'sell' else 'bought'} {crore_shares(d['qty'])} shares"
                     + (f" at ₹{d['price']:,.2f}" if d.get("price") else "") + f" in a {d['kind']} deal on {month(d['date'])}{to}")
    return f"; NSE's {lists}-deal records for {window}: " + "; ".join(parts) + (f"; and {len(deals) - 3} more" if len(deals) > 3 else "") + "."


def changes_words(m, skip=()):
    """'Promoter entities that changed between the two filings: …' or None; entities already named by a SAST filing
    (`skip`) are left out."""
    rows = [c for c in m.get("holder_changes") or [] if not any(same_entity(c["name"], s) for s in skip)]
    if not rows and m.get("unnamed_filings"):
        return (f"The {' and '.join(mon(q) for q in m['unnamed_filings'])} filing (older format) does not mark which named holders are "
                "promoters, so changes by promoter entity cannot be read for this quarter.")
    if not rows:
        return None
    parts = [f"{holder_name(tidy_case(c['name']))} " + (f"{c['before_pct']:.2f}%" if c["before_pct"] is not None else "not listed")
             + " → " + (f"{c['after_pct']:.2f}%" if c["after_pct"] is not None else "not listed") for c in rows[:3]]
    return ("Promoter entities that changed between the two filings: " + "; ".join(parts)
            + (f"; and {len(rows) - 3} more" if len(rows) > 3 else "") + ".")


def trend_shape(rows):
    """Peak, trough and the largest quarter-on-quarter move of the promoter holding (rows newest first).

    First-to-last alone hides the path: Adani Ports read 65.89% (Dec 2024) → 66.03% (Sep 2026), but promoters rose to
    68.02% and then sold about 2 points in the June 2026 quarter."""
    if len(rows) < 2:
        return None
    old = list(reversed(rows))
    peak = max(old, key=lambda r: (r["promoter_pct"], r["quarter"]))
    trough = min(old, key=lambda r: (r["promoter_pct"], r["quarter"]))
    moves = [(b["promoter_pct"] - a["promoter_pct"], a, b) for a, b in zip(old, old[1:])]
    def as_move(m):
        return {"pts": m[0], "from_quarter": m[1]["quarter"], "quarter": m[2]["quarter"]} if m and abs(m[0]) >= 0.005 else None
    rises, falls = [m for m in moves if m[0] > 0], [m for m in moves if m[0] < 0]
    return {"first": old[0], "latest": old[-1], "peak": peak, "trough": trough,
            "largest_move": as_move(max(moves, key=lambda m: abs(m[0]))),
            "largest_rise": as_move(max(rises, key=lambda m: m[0]) if rises else None),
            "largest_fall": as_move(min(falls, key=lambda m: m[0]) if falls else None),
            "flat": abs(peak["promoter_pct"] - trough["promoter_pct"]) < 0.005}


def mon(day):
    return date.fromisoformat(day).strftime("%b %Y")


def trend_line(r):
    t = r.get("promoter_trend_shape")
    if not t:
        return None
    span = f"{mon(t['first']['quarter'])}–{mon(t['latest']['quarter'])}"
    if t["flat"]:
        return f"Promoter holding unchanged at {t['latest']['promoter_pct']:.2f}% over {span}."
    text = (f"Promoter holding over {span}: {t['first']['promoter_pct']:.2f}% → {t['latest']['promoter_pct']:.2f}%, "
            f"peak {t['peak']['promoter_pct']:.2f}% ({mon(t['peak']['quarter'])}), low {t['trough']['promoter_pct']:.2f}% ({mon(t['trough']['quarter'])})")
    # Both directions: the largest move alone (+2.13 pts for Adani Ports) hid a 1.99-point sale two quarters later.
    moves = [f"{word} {m['pts']:+.2f} pts in the quarter to {mon(m['quarter'])}"
             for word, m in (("biggest rise", t.get("largest_rise")), ("biggest fall", t.get("largest_fall"))) if m]
    return text + ("; " + ", ".join(moves) if moves else "") + "."


def holder_name(name):
    """Readable name: 'Sbi Nifty 50 Etf' -> 'SBI Nifty 50 ETF'; fund-house prefixes before ' - ' are dropped."""
    name = name.split(" - ", 1)[-1] if " - " in name and len(name) > 50 else name
    words = []
    for w in name.split():
        up = w.upper().strip(".,()")
        if up in ("SBI", "ETF", "LIC", "HDFC", "ICICI", "UTI", "GQG", "LLP", "LLC", "LP", "PTE", "PVT", "LTD", "II", "III", "IV", "VI", "VII", "VIII", "BV", "B.V.", "PLC", "AG", "NV", "DMCC", "NPS", "DSP", "PGIM", "HSBC", "ONE"):
            words.append(w.upper())  # brackets and stops around an acronym stay: '(SBI' is not 'SBI'
        else:
            words.append(w if not w.isupper() else w.title())
    return " ".join(words)


def compare_label(r):
    """'' when changes are against the previous quarter; otherwise words naming what was (or could not be) compared."""
    prev, used = r.get("previous_quarter"), r.get("compared_quarter")
    if not prev:
        return ""
    if used and used != prev:
        return f"changes are vs {mon(used)} because the {mon(prev)} filing could not be read"
    if not used or not r.get("previous_read", True):
        return f"change vs the previous quarter ({mon(prev)}) unavailable: its filing could not be read"
    return ""


def holders_line(r, limit=6):
    rows = r.get("large_public_holders") or []
    if not rows:
        return None
    read = r.get("previous_read", bool(r.get("previous_quarter")))
    parts = [f"{holder_name(h['name'])} {pct(h['pct'])}" + ("" if h.get("previous_pct") is None else
             f" ({pts(h['pct'] - h['previous_pct'])})" if abs(h["pct"] - h["previous_pct"]) >= 0.005 else "")
             # "new" only when the earlier filing was actually read; an unreadable one proves nothing.
             + (" (new above 1%)" if h.get("previous_pct") is None and read else "") for h in rows[:limit]]
    label = compare_label(r)
    dropped = [d for d in r.get("large_holders_dropped") or []] if read else []
    houses = any(h.get("house") for h in rows[:limit])
    return ("Public holders above 1%: " + "; ".join(parts) + (f"; and {len(rows) - limit} more" if len(rows) > limit else "")
            + (f" ({label})" if label else "") + "."
            + (" No longer named above 1%: " + "; ".join(f"{holder_name(d['name'])} (was {pct(d['pct'])})" for d in dropped[:3])
               + (f"; and {len(dropped) - 3} more" if len(dropped) > 3 else "") + "." if dropped else "")
            + (" Fund houses are compared across quarters as a whole: each filing names a mutual fund or LIC by one of its schemes, "
               "a different one each quarter." if houses else ""))


PROMOTERS_SHOWN = 8  # promoter entities named in the summary; the rest are counted with their combined holding
NOTES_SHOWN = 4      # notes on promoter entities in the summary (all of them are in the full report)


def promoter_holder_line(r):
    """Every promoter entity with its holding (the largest eight, then the rest added up), each with a short note when
    the filing or the curated list of holding vehicles describes it; the parent's share count when one entity holds
    most of it (Reliance Industries in Jio Platforms)."""
    rows = r.get("promoter_holders") or []
    promoter = next((x["pct"] for x in r.get("rows") or [] if x["key"] == "promoter"), None)
    if not rows or not promoter:
        return None
    notes = r.get("promoter_notes") or {}
    top = rows[0]
    total = r.get("total_shares")
    held = top.get("shares")
    shares = (f" ({indian(held)} of {indian(total)} shares)" if held and total else
              f" (about {indian(top['pct'] / 100 * total)} of {indian(total)} shares)" if total else "")
    noted = [h for h in rows[:PROMOTERS_SHOWN] if notes.get(h["name"])]
    about = " ".join(f"{holder_name(h['name'])}: {notes[h['name']]}." for h in noted[:NOTES_SHOWN])
    about += f" (Notes on {len(noted) - NOTES_SHOWN} more in the full report.)" if len(noted) > NOTES_SHOWN else ""
    about = f" About them: {about}" if about else ""
    if len(rows) == 1 and abs(top["pct"] - promoter) < 0.005:
        return f"Promoter: {holder_name(top['name'])} holds all {pct(top['pct'])} of the promoter holding{shares}.{about}"
    shown, rest = rows[:PROMOTERS_SHOWN], rows[PROMOTERS_SHOWN:]
    parts = [f"{holder_name(h['name'])} {pct(h['pct'])}" + (shares if h is top and top["pct"] >= 0.5 * promoter else "") for h in shown]
    tail = f"; and {len(rest)} more promoter entities with {pct(sum(h['pct'] for h in rest))} together" if rest else ""
    return f"Promoter entities ({len(rows)}, promoter group {pct(promoter)} in all): " + "; ".join(parts) + tail + "." + about


def holder_count_text(r):
    if not r.get("holders"):
        return None
    text = f"Shareholders: {indian(r['holders'])}"
    if r.get("previous_holders") and r.get("compared_quarter"):
        text += f" ({indian(r['holders'] - r['previous_holders'], sign=True)} vs {mon(r['compared_quarter'])})"
    return text + "."


def disclosure_text(r):
    """NSE's separate promoter-pledge disclosure as a cross-check, with its dates."""
    d, p = r.get("pledge_disclosure") or {}, r.get("pledge") or {}
    if not d or d.get("pledged_pct_of_promoter") is None:
        return None
    when = f"as of {month(d.get('as_of'))}, updated {month(d.get('updated'))}"
    filed = p.get("pledged_pct_of_promoter")
    if filed is None:
        return f"Pledge figure from NSE's promoter-pledge disclosure ({when})."
    if abs(filed - d["pledged_pct_of_promoter"]) < 0.05:
        return f"NSE's promoter-pledge disclosure ({when}) agrees."
    return f"NSE's promoter-pledge disclosure ({when}) shows {d['pledged_pct_of_promoter']:.2f}% of promoter shares pledged."


def indian(number, sign=False):
    """Whole number with Indian digit grouping: 1070249 -> '10,70,249' (sign=True adds '+' to positives)."""
    digits = str(abs(int(round(number))))
    if len(digits) > 3:
        head, groups = digits[:-3], [digits[-3:]]
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join(groups)
    return ("-" if round(number) < 0 else "+" if sign else "") + digits


def pct(value, digits=2):
    return "n/a" if value is None else f"{value:.{digits}f}%"


def pts(value):
    return "n/a" if value is None else ("unchanged" if abs(value) < 0.005 else f"{value:+.2f} pts")


def month(day):
    return date.fromisoformat(day).strftime("%d %b %Y") if day else "n/a"


def encumbrance_kinds(flags):
    """What the filing ticks besides a pledge: 'non-disposal undertakings', 'other encumbrance' or both (None if neither)."""
    kinds = [w for k, w in (("non_disposal", "non-disposal undertakings (NDUs)"), ("other", "other encumbrance")) if (flags or {}).get(k)]
    return " and ".join(kinds) or None


def pledge_line(r):
    p, d = r.get("pledge") or {}, r.get("pledge_disclosure") or {}
    if r.get("no_promoter"):
        return "No promoter group (a professionally managed company), so there is no promoter holding to pledge."
    pledged = p.get("pledged_pct_of_promoter")
    if pledged is None:
        pledged = d.get("pledged_pct_of_promoter")
    if pledged is None:
        return "Promoter pledge: not available from NSE for this filing."
    text = (f"{pledged:.2f}% of promoter shares are pledged" + (f" ({p['pledged_pct_of_total']:.2f}% of all shares)" if p.get("pledged_pct_of_total") else ""))
    other = p.get("other_encumbered_pct_of_promoter")
    if other:
        kinds = encumbrance_kinds(r.get("encumbrance_flags"))
        # Not a formal pledge: an NDU is a promise to lenders not to sell or pledge the shares, so lenders cannot sell them
        # on a default as they can pledged shares, but the holding is still committed to the borrowing.
        text += (f"; another {other:.2f}% is encumbered without a formal pledge"
                 + (f" (the filing ticks {kinds})" if kinds else " (for example non-disposal undertakings)")
                 + ": a commitment to lenders not to sell or pledge the shares, which lenders cannot sell as they could pledged shares")
    elif other == 0 or not any((r.get("encumbrance_flags") or {}).values()):
        text += "; none otherwise encumbered" if (r.get("encumbrance_flags") or {}) else ""
    return text[0].upper() + text[1:] + "."


def headline(r):
    if r.get("no_promoter"):
        return pledge_line(r)
    promoter = next((x for x in r["rows"] if x["key"] == "promoter"), None)
    lead = (f"Promoters hold {pct(promoter['pct'])}" + (f" ({pts(promoter['change_pts'])} vs {month(r.get('compared_quarter') or r['previous_quarter'])})"
                                                       if promoter["change_pts"] is not None else "")
            if promoter else "Promoter holding not available")
    return f"{lead}. {pledge_line(r)}"


def markdown(r):
    compared = r.get("compared_quarter")
    period = (f"Quarter ended {month(r['quarter'])}" if r.get("filing_kind", "quarterly") == "quarterly"
              else f"Pattern dated {month(r['quarter'])}, filed on listing or for a share issue (not a quarter-end filing)")
    lines = [f"# {r['name']} ({r['symbol']}) — shareholding pattern", "",
             period + f" (filed {month(r['filed'])}" + (", revised" if r.get("revised") else "") + ")"
             + (f" · compared with {month(compared)}" if compared else "") + f" · run {r['as_of'][:16].replace('T', ' ')} IST", "",
             f"**{headline(r)}**", ""]
    if r.get("filing_kind") == "adhoc":
        lines += [adhoc_note(r), ""]
    label = compare_label(r)
    if label:
        lines += [f"⚠ {label[0].upper() + label[1:]}.", ""]
    if r.get("stale"):
        lines += ["⚠ The latest filing is more than about four months old; a newer one may be due.", ""]
    lines += ["| Holder | " + month(r["quarter"]) + " | " + month(compared) + " | Change |", "| --- | ---: | ---: | ---: |"]
    for row in r["rows"]:
        name = ("  ↳ " if row["level"] else "") + row["label"]
        lines.append(f"| {name} | {pct(row['pct'])} | {pct(row['previous_pct'])} | {pts(row['change_pts'])} |")
    if r.get("holders"):
        lines += ["", f"Shareholders: {indian(r['holders'])}" + (f" ({month(compared)}: {indian(r['previous_holders'])})" if r.get("previous_holders") else "") + "."]
    if r.get("promoter_holders"):
        notes = r.get("promoter_notes") or {}
        lines += ["", "## Promoter entities", "", "| Promoter | Holding | Note |", "| --- | ---: | --- |"]
        lines += [f"| {holder_name(h['name'])} | {pct(h['pct'])} | {notes.get(h['name'], '')} |" for h in r["promoter_holders"][:25]]
        if len(r["promoter_holders"]) > 25:
            rest = r["promoter_holders"][25:]
            lines.append(f"| {len(rest)} more | {pct(sum(h['pct'] for h in rest))} | |")
    p, d = r.get("pledge") or {}, r.get("pledge_disclosure") or {}
    lines += ["", "## Promoter pledge and encumbrance", ""]
    if p.get("pledged_shares") is not None:
        lines.append(f"- Pledged (shareholding filing, {month(r['quarter'])}): {indian(p['pledged_shares'])} shares = {pct(p['pledged_pct_of_promoter'])} of promoter holding, "
                     f"{pct(p['pledged_pct_of_total'])} of all shares.")
    if p.get("other_encumbered_shares") is not None:
        lines.append(f"- Otherwise encumbered (non-disposal undertakings and other): {indian(p['other_encumbered_shares'])} shares = {pct(p['other_encumbered_pct_of_promoter'])} of promoter holding.")
    if d:
        lines.append(f"- NSE pledge disclosure (SEBI SAST Reg. 31) as of {month(d['as_of'])}, updated {month(d['updated'])}: "
                     f"{pct(d['pledged_pct_of_promoter'])} of promoter holding, {pct(d['pledged_pct_of_total'])} of all shares.")
    if not p and not d:
        lines.append("- Not available from NSE for this filing.")
    if pledge_trend_line(r):
        lines += ["", pledge_trend_line(r), "",
                  "Pledged share of the promoter holding by quarter: " + " · ".join(
                      (f"{mon(x['quarter'])} {pct(x['pledged_pct'])}" + (f" (all encumbrance {pct(x['encumbered_pct'])})"
                                                                        if x.get("encumbered_pct") and abs(x["encumbered_pct"] - (x["pledged_pct"] or 0)) >= 0.005 else ""))
                      if x["pledged_pct"] is not None else f"{mon(x['quarter'])} {pct(x['encumbered_pct'])} pledged or encumbered (older format)"
                      for x in r["pledge_trend"]) + "."]
    if encumbrance_line(r):
        lines += ["", encumbrance_line(r)]
    if r.get("large_public_holders"):
        lines += ["", "## Public shareholders above 1%", "", f"| Holder | Type | {month(r['quarter'])} | {month(compared)} |",
                  "| --- | --- | ---: | ---: |"]
        def filed(h):
            names = [n for n in h.get("filed_as") or [] if n.upper() != h["name"].upper()]
            return f" (named in the filing as {'; '.join(holder_name(n) for n in names)})" if names else ""
        lines += [f"| {holder_name(h['name'])}{filed(h)} | {h['kind']} | {pct(h['pct'])} | {pct(h.get('previous_pct')) if h.get('previous_pct') is not None else 'below 1% or not named' if r.get('previous_read') else 'n/a'} |"
                  for h in r["large_public_holders"]]
        if r.get("previous_read") and r.get("large_holders_dropped"):
            lines += [f"| {holder_name(d['name'])} | {d.get('kind') or 'n/a'} | below 1% or not named | {pct(d['pct'])} |"
                      for d in r["large_holders_dropped"]]
        if any(h.get("house") for h in r["large_public_holders"]):
            lines += ["", "Mutual funds, LIC and NPS accounts are compared fund house by fund house: each filing names a house's whole holding "
                          "by one of its schemes, and the scheme named changes from quarter to quarter."]
    if r["promoter_trend"]:
        lines += ["", "## Promoter holding by quarter", "", *([trend_line(r), ""] if trend_line(r) else []),
                  *[x for line in move_lines(r) for x in (f"- {line}", "")],
                  " · ".join(f"{date.fromisoformat(t['quarter']).strftime('%b %Y')} {t['promoter_pct']:.2f}%" for t in r["promoter_trend"])]
    if r["errors"]:
        lines += ["", "## Data gaps", ""] + [f"- {v}" for v in r["errors"].values()]
    lines += ["", r["note"], "", f"Sources: {r['source']}. Filing: {r['xbrl'] or 'n/a'}. Research only; not investment advice.", ""]
    return "\n".join(lines)


def adhoc_note(r):
    due = (f" (the first, for the quarter to {month(r['next_quarter_end'])}, is due by {month(r['next_filing_due'])})"
           if r.get("next_quarter_end") else "")
    return (f"⚠ Not a quarter-end filing: NSE lists no quarterly shareholding pattern for {r['symbol']} yet{due}, so this is the "
            "pattern filed on listing or for a share issue.")


def summary(r):
    if r.get("filing_kind") == "adhoc":
        title = (f"{r['name']} ({r['symbol']}) — shareholding pattern dated {month(r['quarter'])} (filed {month(r['filed'])}; "
                 "filed on listing or for a share issue)")
    else:
        title = f"{r['name']} ({r['symbol']}) — shareholding for the quarter ended {month(r['quarter'])} (filed {month(r['filed'])})"
    # The pledge trend sits with today's pledge figure, so the digest stays short.
    lines = [title, " ".join(x for x in (headline(r), disclosure_text(r), pledge_trend_line(r)) if x)]
    if encumbrance_line(r):
        lines.append(encumbrance_line(r))
    if r.get("filing_kind") == "adhoc":
        lines.append(adhoc_note(r))
    owner = promoter_holder_line(r)
    if owner:
        lines.append(owner)
    by_key = {x["key"]: x for x in r["rows"]}
    change = lambda x: f" ({pts(x['change_pts'])})" if x["change_pts"] is not None and abs(x["change_pts"]) >= 0.005 else ""
    parts = []
    for x in r["rows"]:
        if x["level"] or x["key"] == "promoter":
            continue
        text = f"{x['label'].replace(' (FII/FPI)', '').replace(' (DII)', '')} {pct(x['pct'])}{change(x)}"
        if x["key"] == "fii" and (by_key.get("fdi") or {}).get("pct", 0) >= 0.5:
            # Strategic foreign stakes (Meta and Google in Jio Platforms) are not portfolio flows.
            text += f" — of which foreign direct investment {pct(by_key['fdi']['pct'])}{change(by_key['fdi'])}"
        if x["key"] == "dii":
            split = [f"{word} {pct(by_key[k]['pct'])}{change(by_key[k])}" for k, word in (("mf", "mutual funds"), ("insurance", "insurers")) if k in by_key]
            text += f" — {', '.join(split)}" if split else ""
        parts.append(text)
    count = holder_count_text(r)
    label = compare_label(r)
    # Said once: on the holders line when there is one, else here.
    note = f" ({label[0].upper() + label[1:]}.)" if label and not r.get("large_public_holders") else ""
    if parts:
        lines.append("Others: " + "; ".join(parts) + "." + note + (f" {count}" if count else ""))
    elif count or note:
        lines.append(((count or "") + note).strip())
    for line in (trend_line(r), " ".join(move_lines(r)), holders_line(r)):
        if line:
            lines.append(line)
    if r.get("stale"):
        lines.append("⚠ Latest filing is more than about four months old.")
    if r["errors"]:
        lines.append("Data gaps: " + "; ".join(r["errors"].values()) + ".")
    lines.append("Source: NSE shareholding filing (XBRL) and NSE pledge disclosure. Research only; not investment advice.")
    return "\n".join(lines) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Latest shareholding pattern and promoter pledge from NSE filings (research only).")
    p.add_argument("symbol", help="NSE symbol, e.g. ADANIPORTS")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--summary", action="store_true", help="Short answer-first digest")
    p.add_argument("--out", help="Write to a new file")
    a = p.parse_args(argv)
    try:
        report = shareholding(a.symbol)
    except Block as error:
        print(f"BLOCKED - {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, ensure_ascii=False) if a.format == "json" else summary(report) if a.summary else markdown(report)
    if a.out:
        out = Path(a.out)
        if out.exists():
            print("BLOCKED - output exists; choose a new path", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
