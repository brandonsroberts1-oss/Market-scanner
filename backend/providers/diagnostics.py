"""Probe every data source and report exactly what it returned.

When the app shows "no data", the useful question is *why*: DNS, a proxy, a
rate limit, a blocked user agent, a consent redirect, or a changed endpoint.
Each of those needs a different fix, and none of them are distinguishable from
an empty screen.

This module makes one real request per endpoint and reports the HTTP status,
content type, and the first part of the body, plus a plain-language verdict.
Run it with `python launcher.py --diagnose`, or hit /api/diagnostics.
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import asdict, dataclass, field

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


@dataclass
class Probe:
    name: str
    url: str
    status: int | None = None
    ok: bool = False
    elapsed_ms: int | None = None
    content_type: str | None = None
    body_preview: str = ""
    error: str | None = None
    verdict: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    probes: list[Probe] = field(default_factory=list)
    environment: dict = field(default_factory=dict)
    summary: str = ""
    working_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["probes"] = [p.to_dict() for p in self.probes]
        return d


def _resolve(host: str) -> str | None:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return None
    except socket.gaierror as exc:
        return str(exc)


def _interpret(probe: Probe, expect: str) -> None:
    """Turn an HTTP result into something a person can act on."""
    if probe.error:
        if "getaddrinfo" in probe.error or "Name or service" in probe.error:
            probe.verdict = "DNS failed"
            probe.detail = ("The hostname could not be resolved. Check your internet "
                            "connection, or a DNS/VPN issue.")
        elif "timed out" in probe.error.lower() or "timeout" in probe.error.lower():
            probe.verdict = "timed out"
            probe.detail = "No response in time - a firewall or proxy may be dropping it."
        elif "certificate" in probe.error.lower() or "SSL" in probe.error:
            probe.verdict = "TLS failed"
            probe.detail = ("Certificate verification failed. Common on corporate "
                            "networks that inspect traffic.")
        else:
            probe.verdict = "connection failed"
            probe.detail = probe.error
        return

    if probe.status == 200:
        body = probe.body_preview.lstrip()
        looks_html = body.startswith("<") or "text/html" in (probe.content_type or "")
        if expect == "json" and looks_html:
            probe.verdict = "blocked (HTML returned)"
            probe.detail = ("The server answered with a web page instead of data - "
                            "usually a consent screen, a captcha, or a block page.")
        elif expect == "json" and not (body.startswith("{") or body.startswith("[")):
            probe.verdict = "unexpected body"
            probe.detail = "A 200 response that is not JSON."
        else:
            probe.ok = True
            probe.verdict = "OK"
        return

    if probe.status == 401:
        probe.verdict = "401 unauthorised"
        probe.detail = "Authentication rejected (for Yahoo, a missing or stale crumb)."
    elif probe.status == 403:
        probe.verdict = "403 forbidden"
        probe.detail = ("The provider refused the request. Often a blocked user agent, "
                        "region restriction, or a proxy denying the host.")
    elif probe.status == 429:
        probe.verdict = "429 rate limited"
        probe.detail = ("Too many requests. Yahoo throttles unauthenticated use "
                        "aggressively; wait a few minutes, or use Tradier.")
    elif probe.status and 500 <= probe.status < 600:
        probe.verdict = f"{probe.status} server error"
        probe.detail = "The provider is having trouble; try again later."
    else:
        probe.verdict = f"HTTP {probe.status}"
        probe.detail = "Unexpected status."


async def _probe(client: httpx.AsyncClient, name: str, url: str,
                 expect: str = "json", **kwargs) -> Probe:
    probe = Probe(name=name, url=url)
    host = httpx.URL(url).host
    dns_error = _resolve(host)
    if dns_error:
        probe.error = f"getaddrinfo: {dns_error}"
        _interpret(probe, expect)
        return probe

    started = time.monotonic()
    try:
        response = await client.get(url, **kwargs)
        probe.elapsed_ms = int((time.monotonic() - started) * 1000)
        probe.status = response.status_code
        probe.content_type = response.headers.get("content-type", "")
        probe.body_preview = response.text[:220].replace("\n", " ")
    except Exception as exc:                                    # noqa: BLE001
        probe.elapsed_ms = int((time.monotonic() - started) * 1000)
        probe.error = f"{type(exc).__name__}: {exc}"
    _interpret(probe, expect)
    return probe


async def run_diagnostics(symbol: str = "AAPL", timeout: float = 20.0) -> Report:
    """One real request per endpoint, reported honestly."""
    report = Report()
    report.environment = {
        "http_proxy": os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or None,
        "https_proxy": os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None,
        "no_proxy": os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or None,
        "tradier_token_set": bool(os.environ.get("TRADIER_TOKEN", "").strip()),
        "provider_setting": os.environ.get("MARKET_DATA_PROVIDER", "auto"),
    }

    headers = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*",
               "Accept-Language": "en-US,en;q=0.9"}

    async with httpx.AsyncClient(timeout=timeout, headers=headers,
                                 follow_redirects=True) as client:
        # Yahoo: the chart endpoint is the one that historically needs no crumb.
        report.probes.append(await _probe(
            client, "Yahoo chart (history + quote fallback)",
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?range=5d&interval=1d"))

        # Yahoo: cookie + crumb bootstrap, which gates the quote and option paths.
        cookie_probe = await _probe(client, "Yahoo cookie bootstrap",
                                    "https://fc.yahoo.com/", expect="any")
        report.probes.append(cookie_probe)

        crumb_probe = await _probe(client, "Yahoo crumb",
                                   "https://query2.finance.yahoo.com/v1/test/getcrumb",
                                   expect="any")
        report.probes.append(crumb_probe)
        crumb = crumb_probe.body_preview.strip() if crumb_probe.status == 200 else ""
        if crumb and ("<" in crumb or len(crumb) > 40):
            crumb = ""

        suffix = f"&crumb={crumb}" if crumb else ""
        report.probes.append(await _probe(
            client, "Yahoo quote (extended hours)",
            f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbol}{suffix}"))
        report.probes.append(await _probe(
            client, "Yahoo options",
            f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
            f"?{suffix.lstrip('&')}"))

        # CBOE publishes its own delayed option chain with no key or crumb.
        report.probes.append(await _probe(
            client, "CBOE delayed option chain",
            f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"))

        # Stooq serves daily bars as CSV with no auth at all.
        report.probes.append(await _probe(
            client, "Stooq daily bars",
            f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d", expect="csv"))

        if os.environ.get("TRADIER_TOKEN", "").strip():
            token = os.environ["TRADIER_TOKEN"].strip()
            report.probes.append(await _probe(
                client, "Tradier quote",
                f"https://api.tradier.com/v1/markets/quotes?symbols={symbol}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}))

    report.working_sources = [p.name for p in report.probes if p.ok]
    report.summary = _summarise(report)
    return report


def _summarise(report: Report) -> str:
    by_name = {p.name: p for p in report.probes}
    working = report.working_sources

    if not working:
        statuses = {p.verdict for p in report.probes if p.verdict}
        if statuses <= {"DNS failed", "timed out", "connection failed"}:
            return ("Nothing was reachable. This looks like a network problem on this "
                    "machine - no internet, a VPN, or a firewall blocking outbound "
                    "HTTPS - rather than a problem with any data provider.")
        if "403 forbidden" in statuses or "429 rate limited" in statuses:
            return ("Every provider refused the requests (403/429). Your IP is being "
                    "blocked or throttled. A Tradier token avoids this entirely - it "
                    "is a proper authenticated API rather than a scraped endpoint.")
        return ("No data source responded usefully. See the per-endpoint verdicts "
                "below for what each one said.")

    chart = by_name.get("Yahoo chart (history + quote fallback)")
    cboe = by_name.get("CBOE delayed option chain")
    stooq = by_name.get("Stooq daily bars")

    parts = [f"{len(working)} of {len(report.probes)} sources responded."]
    if chart and not chart.ok:
        parts.append("Yahoo's chart endpoint is failing, which is what supplies price "
                     "history and the quote fallback.")
    if cboe and cboe.ok:
        parts.append("CBOE is reachable, so option chains can be served from there.")
    if stooq and stooq.ok:
        parts.append("Stooq is reachable, so daily price history can be served from there.")
    return " ".join(parts)


def format_report(report: Report) -> str:
    """Plain-text rendering for the terminal."""
    lines = ["", "=" * 68, "  MARKET SCANNER - DATA SOURCE DIAGNOSTICS", "=" * 68, ""]

    env = report.environment
    lines.append("  Environment")
    lines.append(f"    provider setting : {env.get('provider_setting')}")
    lines.append(f"    Tradier token    : {'set' if env.get('tradier_token_set') else 'not set'}")
    if env.get("https_proxy") or env.get("http_proxy"):
        lines.append(f"    proxy            : {env.get('https_proxy') or env.get('http_proxy')}")
    lines.append("")

    for probe in report.probes:
        mark = "OK  " if probe.ok else "FAIL"
        timing = f"{probe.elapsed_ms}ms" if probe.elapsed_ms is not None else "-"
        lines.append(f"  [{mark}] {probe.name}  ({timing})")
        lines.append(f"         {probe.url[:96]}")
        lines.append(f"         -> {probe.verdict}")
        if probe.detail:
            lines.append(f"         {probe.detail}")
        if not probe.ok and probe.body_preview:
            lines.append(f"         body: {probe.body_preview[:140]}")
        lines.append("")

    lines.append("  " + "-" * 64)
    lines.append("  VERDICT")
    lines.append("  " + "-" * 64)
    for chunk in _wrap(report.summary, 64):
        lines.append(f"  {chunk}")
    lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]
