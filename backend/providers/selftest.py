"""End-to-end check of the real provider code, not just connectivity.

An HTTP probe that reports "200 OK" proves the server answered. It does not
prove this app understood the answer. A provider whose field names are wrong
returns 200, parses nothing, and looks identical to a working one from the
outside - while the app shows no data.

This runs each provider's actual quotes/history/expirations/chain methods and
reports how many records came back, with a sample. When a parser finds
nothing it also dumps the payload's real keys, which is what identifies a
field-name mismatch in one run instead of several rounds of guessing.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


@dataclass
class Check:
    source: str
    capability: str
    ok: bool = False
    count: int = 0
    sample: str = ""
    error: str = ""
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SelfTest:
    symbol: str
    checks: list[Check] = field(default_factory=list)
    payload_keys: dict = field(default_factory=dict)
    verdict: str = ""
    usable: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["checks"] = [c.to_dict() for c in self.checks]
        return d


async def _probe_raw_keys(symbol: str) -> dict:
    """Fetch each vendor payload and record its top-level shape.

    This is what tells us a parser is looking for the wrong field rather than
    the server being down.
    """
    out: dict = {}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                 headers={"User-Agent": UA}) as client:
        try:
            r = await client.get(
                f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json")
            if r.status_code == 200:
                body = r.json()
                data = body.get("data") if isinstance(body, dict) else None
                out["cboe"] = {
                    "top_level_keys": sorted(body.keys())[:15] if isinstance(body, dict) else str(type(body)),
                    "data_keys": sorted(data.keys())[:25] if isinstance(data, dict) else None,
                    "option_count": len(data.get("options") or []) if isinstance(data, dict) else 0,
                    "first_option_keys": (sorted((data["options"][0] or {}).keys())
                                          if isinstance(data, dict) and data.get("options") else None),
                    "first_option": (json.dumps(data["options"][0])[:300]
                                     if isinstance(data, dict) and data.get("options") else None),
                }
            else:
                out["cboe"] = {"http_status": r.status_code}
        except Exception as exc:                                # noqa: BLE001
            out["cboe"] = {"error": f"{type(exc).__name__}: {exc}"}

        try:
            r = await client.get(f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d")
            text = r.text.strip()
            lines = text.splitlines()
            out["stooq"] = {
                "http_status": r.status_code,
                "header": lines[0] if lines else "",
                "rows": max(len(lines) - 1, 0),
                "last_row": lines[-1] if len(lines) > 1 else "",
            }
        except Exception as exc:                                # noqa: BLE001
            out["stooq"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


async def _check(source: str, capability: str, coro, describe, provider=None) -> Check:
    check = Check(source=source, capability=capability)
    try:
        result = await asyncio.wait_for(coro, timeout=45.0)
    except asyncio.TimeoutError:
        check.error = "timed out after 45s"
        return check
    except Exception as exc:                                    # noqa: BLE001
        check.error = f"{type(exc).__name__}: {exc}"
        return check

    try:
        count, sample = describe(result)
    except Exception as exc:                                    # noqa: BLE001
        check.error = f"could not read the result: {exc}"
        return check

    check.count = count
    check.sample = sample
    check.ok = count > 0
    if not check.ok:
        # Distinguish "could not reach it" from "reached it and understood
        # nothing" - they need completely different fixes.
        vendor_error = getattr(provider, "last_error", None)
        if vendor_error:
            check.error = str(vendor_error)
            check.hint = "The source did not return usable data for this request."
        else:
            check.hint = ("The request succeeded but this app parsed nothing from "
                          "it - that points at a field-name mismatch in this app, "
                          "not an outage.")
    return check


async def run_selftest(symbol: str = "AAPL") -> SelfTest:
    """Exercise every provider the way the app does, and report what came back."""
    from .cboe import CboeProvider
    from .stooq import StooqProvider
    from .yahoo import YahooProvider

    report = SelfTest(symbol=symbol.upper())
    report.payload_keys = await _probe_raw_keys(symbol.upper())

    providers = [YahooProvider(), CboeProvider(), StooqProvider()]
    try:
        for provider in providers:
            name = provider.name

            report.checks.append(await _check(
                name, "quotes", provider.quotes([symbol]),
                lambda q: (len(q), (f"{symbol} = {list(q.values())[0].last}"
                                    if q else "nothing parsed")),
                provider,
            ))

            report.checks.append(await _check(
                name, "history", provider.history(symbol, 120),
                lambda b: (len(b), (f"{len(b)} bars, latest {b.bars[-1].date} "
                                    f"close {b.bars[-1].close}") if len(b) else "no bars"),
                provider,
            ))

            expirations: list[str] = []

            def note_expirations(e):
                expirations.extend(e or [])
                return (len(e or []), ", ".join((e or [])[:4]) or "none listed")

            report.checks.append(await _check(
                name, "expirations", provider.expirations(symbol), note_expirations,
                provider))

            if expirations:
                report.checks.append(await _check(
                    name, "option chain", provider.chain(symbol, expirations[0]),
                    lambda c: ((len(c.calls) + len(c.puts)) if c else 0,
                               (f"{len(c.calls)} calls / {len(c.puts)} puts at "
                                f"spot {c.underlying_price}") if c else "no chain"),
                    provider,
                ))
            else:
                report.checks.append(Check(name, "option chain",
                                           error="skipped - no expirations to ask for"))
    finally:
        for provider in providers:
            try:
                await provider.close()
            except Exception:                                   # noqa: BLE001
                pass

    report.usable = _capability_summary(report)
    report.verdict = _verdict(report)
    return report


def _capability_summary(report: SelfTest) -> dict:
    usable: dict[str, list[str]] = {}
    for check in report.checks:
        if check.ok:
            usable.setdefault(check.capability, []).append(check.source)
    return usable


def _verdict(report: SelfTest) -> str:
    usable = report.usable
    needed = {"quotes": "quotes", "history": "price history",
              "expirations": "option expirations", "option chain": "option chains"}
    missing = [label for key, label in needed.items() if not usable.get(key)]

    parsed_nothing = [f"{c.source}/{c.capability}" for c in report.checks
                      if not c.ok and not c.error]
    unreachable = sorted({c.source for c in report.checks if c.error})
    if not missing:
        return ("Every capability the scanner needs is available: "
                + "; ".join(f"{k} from {', '.join(v)}" for k, v in usable.items()) + ".")

    parts = [f"Missing: {', '.join(missing)}."]
    if unreachable:
        parts.append(f"Could not get usable data from: {', '.join(unreachable)}.")
    if parsed_nothing:
        parts.append("These returned data this app could not parse, which points at "
                     "a field-name mismatch rather than an outage: "
                     + ", ".join(parsed_nothing) + ".")
    return " ".join(parts)


def format_selftest(report: SelfTest) -> str:
    lines = ["", "=" * 72,
             f"  PROVIDER SELF-TEST - running the real code against {report.symbol}",
             "=" * 72, ""]

    current = None
    for check in report.checks:
        if check.source != current:
            current = check.source
            lines.append(f"  {current.upper()}")
        mark = "OK  " if check.ok else "FAIL"
        detail = check.sample or check.error or "nothing"
        lines.append(f"    [{mark}] {check.capability:14s} {check.count:>5}  {detail[:74]}")
        if check.hint:
            lines.append(f"           {check.hint}")
    lines.append("")

    lines.append("  RAW PAYLOAD SHAPE (what the vendors actually sent)")
    for source, info in report.payload_keys.items():
        lines.append(f"    {source}:")
        for key, value in (info or {}).items():
            text = str(value)
            lines.append(f"      {key}: {text[:150]}")
    lines.append("")

    lines.append("  " + "-" * 68)
    lines.append("  VERDICT")
    lines.append("  " + "-" * 68)
    for chunk in _wrap(report.verdict, 68):
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
