#!/usr/bin/env python3
"""
refresh_jim_briefing.py — regenerate jim/index.html from the latest zombie-shell briefing.

Usage:
    python3 refresh_jim_briefing.py \
        --briefings-dir "/path/to/Claude CoWork Stock Analyst/briefings/Documents" \
        --repo-dir      "/path/to/ai-pivot-explainer" \
        [--commit] [--push]

Design:
    The repo holds jim/template.html with {{PLACEHOLDERS}}.
    This script reads the most-recent zombie-shell-YYYY-MM-DD.md, extracts the
    CHAT-FIRST SUMMARY block (for the headline callout) and the body of the
    scan (HOT CANDIDATES + PRE-ANNOUNCEMENT WATCH + COPYCAT WINDOW + SECTOR
    TREND UPDATE + MISSES). Converts the relevant markdown to HTML using
    a small inline renderer (no external deps), substitutes into the template,
    writes jim/index.html, and optionally commits + pushes.

    Run from a cron / scheduled task each weekday morning around 7:25 AM ET,
    then fire the Jim email immediately after.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import pathlib
import re
import subprocess
import sys


# ---------- markdown -> minimal HTML ----------

def md_to_html(md: str) -> str:
    """Tiny markdown renderer covering exactly what the briefings use:
    - H3 (### )
    - bold **x**, italics *x*
    - inline code `x`
    - bullet lists (- )
    - simple pipe tables
    - blank-line paragraph splitting
    """
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Table block: header row + |---|---|---|
        if line.startswith('|') and i + 1 < len(lines) and re.match(r'^\|[\s\-:|]+\|$', lines[i + 1]):
            header_cells = [c.strip() for c in line.strip('|').split('|')]
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].startswith('|'):
                rows.append([c.strip() for c in lines[i].strip('|').split('|')])
                i += 1
            out.append('<table><thead><tr>' + ''.join(f'<th>{_inline(c)}</th>' for c in header_cells) + '</tr></thead><tbody>')
            for r in rows:
                out.append('<tr>' + ''.join(f'<td>{_inline(c)}</td>' for c in r) + '</tr>')
            out.append('</tbody></table>')
            continue
        # H3
        if line.startswith('### '):
            out.append(f'<h3>{_inline(line[4:].strip())}</h3>')
            i += 1
            continue
        # Bullet list
        if line.startswith('- '):
            out.append('<ul class="clean">')
            while i < len(lines) and lines[i].startswith('- '):
                out.append(f'<li>{_inline(lines[i][2:].strip())}</li>')
                i += 1
            out.append('</ul>')
            continue
        # Numbered list
        if re.match(r'^\d+\.\s', line):
            out.append('<ol class="clean">')
            num_re = re.compile(r'^\d+\.\s')
            while i < len(lines) and num_re.match(lines[i]):
                item_text = num_re.sub('', lines[i]).strip()
                out.append(f'<li>{_inline(item_text)}</li>')
                i += 1
            out.append('</ol>')
            continue
        # Horizontal rule
        if line.strip() == '---':
            i += 1
            continue
        # Blank line
        if not line.strip():
            i += 1
            continue
        # Plain paragraph (consume until blank)
        para = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i]):
            para.append(lines[i])
            i += 1
        out.append('<p>' + _inline(' '.join(para)) + '</p>')
    return '\n'.join(out)


def _is_block_start(line: str) -> bool:
    return (
        line.startswith('### ')
        or line.startswith('- ')
        or line.startswith('|')
        or bool(re.match(r'^\d+\.\s', line))
        or line.strip() == '---'
    )


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)
    # Tradability pills
    text = text.replace('TRADABLE', '<span class="pill tradable">TRADABLE</span>')
    text = text.replace('RESTRICTED', '<span class="pill restricted">RESTRICTED</span>')
    return text


# ---------- briefing extraction ----------

CHAT_FIRST_RE = re.compile(
    r'## CHAT-FIRST SUMMARY.*?\n(.*?)(?=\n---|\n## )', re.DOTALL
)

# Match the briefing section headers regardless of capitalization or trailing qualifier.
# (e.g. "## 🔴 Hot Candidates — Actionable Today" matches the "Hot Candidates" key.)
SECTIONS_OF_INTEREST = [
    ('🔴', 'Hot Candidates'),
    ('🟡', 'Pre-Announcement Watch'),
    ('🧨', 'Copycat Window'),
    ('🌍', 'Foreign-ADS Pivot Watch'),
    ('📊', 'Pre-Market Gainers'),
    ('🟢', 'Sector Trend Update'),
    ('⚠️', 'Misses'),
]

# Terms that, if present, mark a paragraph as Mike-specific and should be stripped
# from anything sent to Jim. Per feedback_jim_brief_public_only.md (5/12) +
# feedback_jim_email_template_leak.md and the 5/15 revision.
PORTFOLIO_LEAK_TERMS = [
    "mike's", "mike ", "mike,", "mike.", "mike's gig",
    'portfolio', 'cost basis', 'unrealized', 'p/l', 'cash',
    'inh ira', 'inherited ira', 'schwab ira', 'robinhood',
    "mike's book", 'carry-over', 'his $', 'his position',
    'sized appropriately', 'csv', 'positions snapshot',
    'sh @', 'shares @ avg', 'avg cost', 'his cost',
    'london bridge', 'psychedelics basket', 'barrel 4',
    'ionq 40', 'gig 75', 'mtz 3', 'etn 2', 'mp 33', 'remx 11',
    'open orders', 'buy limit', 'stop placed', 'trim half',
    'trailing stop', 'fill', 'order status',
]


def _strip_portfolio_leaks(md: str) -> str:
    """Drop any paragraph or bullet that mentions Mike-specific positions, P/L, or cash."""
    out_lines: list[str] = []
    paragraph: list[str] = []
    def flush():
        if not paragraph:
            return
        joined = ' '.join(paragraph).lower()
        if not any(term in joined for term in PORTFOLIO_LEAK_TERMS):
            out_lines.extend(paragraph)
        paragraph.clear()
    for line in md.splitlines():
        if not line.strip():
            flush()
            out_lines.append(line)
            continue
        paragraph.append(line)
    flush()
    return '\n'.join(out_lines)


def extract_headline_oneliner(briefing_md: str) -> str:
    """Pull the first bullet from CHAT-FIRST SUMMARY for the email subject/h1."""
    m = CHAT_FIRST_RE.search(briefing_md)
    if not m:
        return 'Today\'s scan'
    first_line = next(
        (ln for ln in m.group(1).splitlines() if ln.lstrip().startswith(('1.', '- '))),
        ''
    )
    # Strip leading "1." / "- " and any markdown bold
    txt = re.sub(r'^\s*(?:\d+\.|-)\s*', '', first_line)
    txt = re.sub(r'\*\*([^*]+)\*\*', r'\1', txt)
    # Trim to ~90 chars for subject line
    if len(txt) > 90:
        txt = txt[:87].rsplit(' ', 1)[0] + '...'
    return txt.strip() or 'Today\'s scan'


JIM_SUMMARY_RE = re.compile(
    r'## JIM SUMMARY.*?\n(.*?)(?=\n---|\n## )', re.DOTALL
)


def extract_headline(briefing_md: str) -> str:
    # Prefer an explicit JIM SUMMARY block authored zombie-only.
    m = JIM_SUMMARY_RE.search(briefing_md)
    if m:
        return md_to_html(m.group(1).strip())
    # Fallback: CHAT-FIRST SUMMARY with portfolio leaks stripped.
    m = CHAT_FIRST_RE.search(briefing_md)
    if not m:
        return '<p>No JIM SUMMARY or CHAT-FIRST SUMMARY found in latest briefing.</p>'
    return md_to_html(_strip_portfolio_leaks(m.group(1).strip()))


def _section_pattern(emoji: str, key: str) -> re.Pattern:
    # Strip optional Unicode variation selector (U+FE0F) so the regex matches whether or not
    # the markdown source uses the emoji-presentation form. The first emoji char is the base.
    base = emoji[0]
    # NOTE: \uFE0F (variation selector) needs a real-string escape, not a raw-string one.
    # In a raw string r'\uFE0F' is six literal chars, not U+FE0F.
    return re.compile(
        r'^##\s+' + re.escape(base) + '\uFE0F?' + r'\s+' + re.escape(key) + r'[^\n]*\n(.*?)(?=\n## |\Z)',
        re.DOTALL | re.MULTILINE | re.IGNORECASE,
    )


def _is_substantive(text: str) -> bool:
    # After leak-stripping we need to filter out sections that became essentially empty
    # (only horizontal rules, blank lines, or a single short line).
    stripped = re.sub(r'^[-=\s]+$', '', text, flags=re.MULTILINE).strip()
    return len(stripped) >= 80


def extract_scan_body(briefing_md: str) -> str:
    out_parts: list[str] = []
    for emoji, key in SECTIONS_OF_INTEREST:
        m = _section_pattern(emoji, key).search(briefing_md)
        if not m:
            continue
        body = _strip_portfolio_leaks(m.group(1).strip())
        if not _is_substantive(body):
            continue
        out_parts.append(f'<h3>{key}</h3>')
        out_parts.append(md_to_html(body))
    return '\n\n'.join(out_parts) if out_parts else '<p>No scan sections extracted.</p>'




# ---------- email send ----------

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr


import os; _WORKSPACE_DIRS = list(pathlib.Path('/sessions').glob('*/mnt/Claude CoWork Stock Analyst/.secrets'))
DEFAULT_PASSWORD_FILE = pathlib.Path(os.environ.get('JIM_GMAIL_PASSWORD_FILE') or (str(_WORKSPACE_DIRS[0] / 'gmail_app_password.txt') if _WORKSPACE_DIRS else str(pathlib.Path.home() / '.jim-secrets' / 'gmail_app_password.txt')))

SMTP_HOST = 'smtp.gmail.com'
SMTP_PORT = 587  # STARTTLS
SENDER_ADDR = 'fructifyme@gmail.com'
SENDER_NAME = 'Mike Farley'
JIM_ADDR = 'jfarley24o3@comcast.net'


def load_password(password_file: pathlib.Path) -> str:
    if not password_file.exists():
        raise SystemExit(
            f'Password file not found: {password_file}\n'
            f'Create it with the MSN app password from account.live.com/proofs/AppPassword.\n'
            f'Run: mkdir -p {password_file.parent} && chmod 700 {password_file.parent} && '
            f'echo YOUR_APP_PASSWORD > {password_file} && chmod 600 {password_file}'
        )
    return password_file.read_text(encoding='utf-8').strip()


def build_message(html_body: str, *, to_addr: str, subject: str) -> EmailMessage:
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = formataddr((SENDER_NAME, SENDER_ADDR))
    msg['To'] = to_addr
    # Plain-text fallback for clients that don't render HTML
    msg.set_content(
        f'Your daily brief is best viewed in HTML. '
        f'Open this email in a browser or in a modern mail client. '
        f'Live archive: https://fructifyme.github.io/ai-pivot-explainer/jim/\n\n— Mike'
    )
    msg.add_alternative(html_body, subtype='html')
    return msg


def send_email(msg: EmailMessage, *, password: str) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.ehlo()
        s.starttls(context=context)
        s.ehlo()
        s.login(SENDER_ADDR, password)
        s.send_message(msg)


# ---------- main flow ----------

def find_latest_briefing(briefings_dir: pathlib.Path) -> pathlib.Path:
    files = sorted(briefings_dir.glob('zombie-shell-*.md'))
    if not files:
        raise SystemExit(f'No zombie-shell-*.md files found in {briefings_dir}')
    return files[-1]


def render(template: str, **subs: str) -> str:
    out = template
    for k, v in subs.items():
        out = out.replace('{{' + k.upper() + '}}', v)
    return out


def git(repo: pathlib.Path, *args: str) -> None:
    subprocess.check_call(['git', '-C', str(repo), *args])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--briefings-dir', required=True, type=pathlib.Path)
    p.add_argument('--repo-dir', required=True, type=pathlib.Path)
    p.add_argument('--commit', action='store_true', help='git add + commit the result')
    p.add_argument('--push', action='store_true', help='git push (implies --commit)')
    p.add_argument('--send', action='store_true', help='actually email the brief via SMTP')
    p.add_argument('--dry-run-to-me', action='store_true',
                   help='with --send, send to mikefarley@msn.com instead of Jim — for testing')
    p.add_argument('--password-file', type=pathlib.Path, default=DEFAULT_PASSWORD_FILE,
                   help='Path to file containing the MSN app password')
    args = p.parse_args()

    latest = find_latest_briefing(args.briefings_dir)
    scan_date_match = re.search(r'zombie-shell-(\d{4}-\d{2}-\d{2})\.md', latest.name)
    scan_date = scan_date_match.group(1) if scan_date_match else 'unknown'

    briefing_md = latest.read_text(encoding='utf-8')
    headline_html = '<div class="callout info"><h3>Read this first</h3>' + extract_headline(briefing_md) + '</div>'
    scan_body_html = extract_scan_body(briefing_md)

    page_template_path = args.repo_dir / 'jim' / 'template.html'
    email_template_path = args.repo_dir / 'jim' / 'email-template.html'
    for p in (page_template_path, email_template_path):
        if not p.exists():
            raise SystemExit(f'Template not found at {p}')
    page_template = page_template_path.read_text(encoding='utf-8')
    email_template = email_template_path.read_text(encoding='utf-8')

    now = dt.datetime.now()
    common = dict(
        edition_date=now.strftime('%Y-%m-%d'),
        refresh_time=now.strftime('%H:%M ET (auto)'),
        scan_date=scan_date,
        headline_html=headline_html,
        scan_body_html=scan_body_html,
        headline_one_liner=extract_headline_oneliner(briefing_md),
    )

    page_path = args.repo_dir / 'jim' / 'index.html'
    page_path.write_text(render(page_template, **common), encoding='utf-8')
    print(f'Wrote {page_path}')

    email_path = args.repo_dir / 'jim' / 'email.html'
    email_html = render(email_template, **common)
    # Inline-style the pills so Outlook etc. render them correctly (they ignore <style>)
    email_html = email_html.replace(
        '<span class="pill tradable">TRADABLE</span>',
        '<span style="display:inline-block;background:#dcfce7;color:#15803d;border:1px solid #bbf7d0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;padding:1px 8px;border-radius:99px;letter-spacing:0.05em;font-weight:600">TRADABLE</span>'
    ).replace(
        '<span class="pill restricted">RESTRICTED</span>',
        '<span style="display:inline-block;background:#fef3c7;color:#b45309;border:1px solid #fde68a;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;padding:1px 8px;border-radius:99px;letter-spacing:0.05em;font-weight:600">RESTRICTED</span>'
    )
    email_path.write_text(email_html, encoding='utf-8')
    print(f'Wrote {email_path} (pass --send to email it via SMTP)')

    if args.commit or args.push:
        git(args.repo_dir, 'add', 'jim/index.html', 'jim/email.html')
        msg = f'Jim daily refresh — {now.strftime("%Y-%m-%d")} (from {latest.name})'
        try:
            git(args.repo_dir, 'commit', '-m', msg)
        except subprocess.CalledProcessError:
            print('No changes to commit.')
            return
        if args.push:
            git(args.repo_dir, 'push')
            print('Pushed.')

    if args.send:
        password = load_password(args.password_file)
        to_addr = SENDER_ADDR if args.dry_run_to_me else JIM_ADDR
        subject = f"Jim's Daily Brief — {common['edition_date']} · {common['headline_one_liner'][:60]}"
        msg = build_message(email_path.read_text(encoding='utf-8'),
                            to_addr=to_addr, subject=subject)
        try:
            send_email(msg, password=password)
            print(f'Sent to {to_addr}: "{subject}"')
        except Exception as e:
            print(f'SEND FAILED: {type(e).__name__}: {e}')
            raise SystemExit(2)


if __name__ == '__main__':
    main()
