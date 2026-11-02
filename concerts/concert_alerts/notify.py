import shutil
import subprocess
from collections import defaultdict

from .events import notification, telegram_html
from .library import name_key


def group_key(event):
    artists = tuple(sorted(event["artists"]))
    return artists, name_key(event["name"]) if len(artists) > 1 else None


def select_digest_dates(entries, catalogue, max_nationwide_dates=3):
    dates = defaultdict(set)
    for event in catalogue:
        for artist in event["artists"]:
            dates[artist].add(event["id"])
    visible, hidden = [], []
    for entry in entries:
        event = entry[0]
        frequent = bool(event["artists"]) and all(
            len(dates[artist]) > max_nationwide_dates for artist in event["artists"])
        if max_nationwide_dates and frequent and not event["highlight"]:
            hidden.append(entry)
        else:
            visible.append(entry)
    return visible, hidden


def batches(entries, limit=3800, lookahead_days=365, catalogue=None, max_nationwide_dates=3):
    entries = list(entries)
    catalogue = list(catalogue) if catalogue is not None else [entry[0] for entry in entries]
    entries, _ = select_digest_dates(entries, catalogue, max_nationwide_dates)
    _, hidden_dates = select_digest_dates([(event, False, "") for event in catalogue], catalogue, max_nationwide_dates)
    hidden_counts = defaultdict(int)
    for event, _, _ in hidden_dates:
        hidden_counts[group_key(event)] += 1
    groups = {}
    for entry in entries:
        event = entry[0]
        # Solo matches share an artist block; multi-artist bills stay together.
        key = group_key(event)
        groups.setdefault(key, []).append(entry)
    updated = sum(bool(entry[1]) for entry in entries)
    counts = []
    if len(entries) - updated:
        new = len(entries) - updated
        counts.append(f"{new} new {'date' if new == 1 else 'dates'}")
    if updated:
        counts.append(f"{updated} updated {'date' if updated == 1 else 'dates'}")
    summary = f"{', '.join(counts)}\nNext {lookahead_days} days · ★ preferred areas · local times"
    sources = sorted({telegram_html(entry[0]["source"], 80) for entry in entries})
    footer = "\n\nSource: " + ", ".join(sources)
    pages, batch, blocks = [], [], []

    def header(page, total):
        title = f"Concerts · {page}/{total}"
        return title + (" · " + summary if page == 1 else "")

    def fits(text):
        # Use the maximum possible page count until the final packing is known.
        envelope = header(len(pages) + 1, len(entries)) + "\n\n" + text + footer
        return len(envelope.encode("utf-16-le")) // 2 <= limit

    def flush():
        if batch:
            pages.append((batch.copy(), "\n\n".join(blocks)))
            batch.clear()
            blocks.clear()

    for group in groups.values():
        group.sort(key=lambda entry: (entry[0]["date_unknown"], entry[0]["date"], entry[0]["time"], entry[0]["id"]))
        event = group[0][0]
        artists = ", ".join(event["artists"].values())
        title = telegram_html(artists, 300)
        common_title = len({name_key(entry[0]["name"]) for entry in group}) == 1
        if common_title and name_key(artists) != name_key(event["name"]):
            title += " — " + telegram_html(event["name"], 200)
        hidden = hidden_counts[group_key(event)]
        if len(group) > 1 or hidden:
            title += f" · {len(group)} {'date' if len(group) == 1 else 'dates'}"
        if hidden:
            title += f" · {hidden} elsewhere hidden"
        rows = [notification(entry[0], entry[1], show_title=(not common_title and name_key(entry[0]["name"]) != name_key(artists)))
                for entry in group]
        block = f"<b>{title}</b>\n" + "\n".join(rows)
        if not fits("\n\n".join(blocks + [block])):
            flush()
        if fits(block):
            blocks.append(block)
            batch.extend(group)
            continue
        # Only a group too large for an empty message may span messages.
        partial, partial_rows, continued = [], [], False
        for entry, row in zip(group, rows):
            group_title = title + (" (continued)" if continued else "")
            candidate = f"<b>{group_title}</b>\n" + "\n".join(partial_rows + [row])
            if partial and not fits(candidate):
                blocks.append(f"<b>{group_title}</b>\n" + "\n".join(partial_rows))
                batch.extend(partial)
                flush()
                partial, partial_rows, continued = [], [], True
                candidate = f"<b>{title} (continued)</b>\n" + row
            if not fits(candidate):
                raise ValueError("Digest message limit is too small for an event")
            partial.append(entry)
            partial_rows.append(row)
        group_title = title + (" (continued)" if continued else "")
        blocks.append(f"<b>{group_title}</b>\n" + "\n".join(partial_rows))
        batch.extend(partial)
    flush()
    for index, (page_entries, body) in enumerate(pages, 1):
        text = header(index, len(pages)) + "\n\n" + body
        yield page_entries, text + (footer if index == len(pages) else "")


def send_telegram(text, executable="hermes"):
    command = shutil.which(executable)
    if not command:
        raise RuntimeError("Hermes is unavailable; notification remains pending")
    try:
        result = subprocess.run([command, "send", "-q", "--to", "telegram", text],
                                capture_output=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Telegram delivery failed; notification remains pending") from None
    if result.returncode:
        raise RuntimeError("Hermes could not confirm delivery; notification remains pending")


def deliver(pending, state, send=send_telegram, lookahead_days=365,
            catalogue=None, max_nationwide_dates=3):
    sent = 0
    for entries, text in batches(pending, lookahead_days=lookahead_days,
                               catalogue=catalogue, max_nationwide_dates=max_nationwide_dates):
        send(text)
        state.delivered(entries)
        sent += len(entries)
    return sent
