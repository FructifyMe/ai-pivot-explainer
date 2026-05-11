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

SECTIONS_OF_INTEREST = [
    '## 🔴 HOT CANDIDATES',
    '## 🟡 PRE-ANNOUNCEMENT WATCH',
    '## 🧨 COPYCAT WINDOW ALERT',
    '## 📊 PRE-MARKET GAINERS',
    '## 🟢 SECTOR TREND UPDATE',
    '## ⚠️ MISSES',
]


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


def extract_headline(briefing_md: str) -> str:
    m = CHAT_FIRST_RE.search(briefing_md)
    if not m:
        return '<p>No CHAT-FIRST SUMMARY found in latest briefing.</p>'
    return md_to_html(m.group(1).strip())


def extract_scan_body(briefing_md: str) -> str:
    out_parts: list[str] = []
    for section in SECTIONS_OF_INTEREST:
        pattern = re.compile(
            re.escape(section) + r'.*?\n(.*?)(?=\n## |\Z)', re.DOTALL
        )
        m = pattern.search(briefing_md)
        if not m:
            continue
        # Strip the emoji / heading and turn into an h3 sub-section
        nice_heading = re.sub(r'^## [^\w]+', '', section).strip()
        out_parts.append(f'<h3>{nice_heading}</h3>')
        out_parts.append(md_to_html(m.group(1).strip()))
    return '\n\n'.join(out_parts) if out_parts else '<p>No scan sections extracted.</p>'


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
    email_path.write_text(render(email_template, **common), encoding='utf-8')
    print(f'Wrote {email_path} (paste into Gmail compose, or load with --send via SMTP — not yet implemented)')

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


if __name__ == '__main__':
    main()
