import datetime as dt

import board

TODAY = dt.date(2026, 9, 26)

QUEUE = """# queue

## Open

| Added | Project | Item | Days open | Done |
|---|---|---|---|---|
| 2026-09-01 | hestia | **12:00 or 15:00: reply in the forum thread** about languages | 0 | |
| 2026-09-20 | garden | **Morning, greenhouse: check the marigold seed bag** for the type | 0 | |
| 2026-09-20 | garden | **Morning: pot the rosemary** | 0 | 2026-09-21 (done) |
| 2026-09-20 | blog | **12:00 or 15:00 on or after 2026-10-04: check the submission** | 0 | |
| 2026-09-20 | blog | **Late November, morning: photograph the winterizing** | 0 | |
| 2026-08-10 | hestia | Tailscale ACL restricting port 8730 — own devices only | 0 | |

## Shipped

| Done | Project | What |
|---|---|---|
| 2026-09-02 | hestia | **Morning: something finished** |
"""


def test_queue_reads_only_open_unfinished_current_rows():
    items = board.queue_items(QUEUE, TODAY)
    titles = [i["title"] for i in items]
    assert titles == ["Tailscale ACL restricting port 8730",
                      "Reply in the forum thread",
                      "Check the marigold seed bag"]


def test_slot_prefix_is_stripped_and_column_follows_it():
    items = {i["title"]: i for i in board.queue_items(QUEUE, TODAY)}
    assert items["Reply in the forum thread"]["column"] == "screen"
    assert items["Check the marigold seed bag"]["column"] == "hands"
    assert items["Tailscale ACL restricting port 8730"]["column"] == "screen"


def test_age_is_counted_from_added():
    items = board.queue_items(QUEUE, TODAY)
    assert items[0]["age"] == 47


def test_deferred_rows_come_back_on_their_date():
    later = board.queue_items(QUEUE, dt.date(2026, 11, 20))
    titles = [i["title"] for i in later]
    assert "Check the submission" in titles
    assert "Photograph the winterizing" in titles


def test_hands_only_means_nobody_else_not_physical():
    assert board.column_of("Set up sudo on the boxes. Root on each, so it is hands-only",
                           "Set up sudo on the boxes") == "screen"


def test_mood_follows_the_most_urgent_home_item():
    noon = dt.datetime(2026, 9, 26, 12, 0)
    night = dt.datetime(2026, 9, 26, 23, 0)
    urgent = [{"title": "Box: no reading", "level": 2}]
    today = [{"title": "Weigh the pups", "level": 1}]
    assert board.mood(urgent, night) == "concerned"   # urgent wakes it
    assert board.mood(today, noon) == "attentive"
    assert board.mood(today, night) == "asleep"
    assert board.mood([], noon) == "content"
    assert board.headline(urgent, "concerned") == "Box: no reading"


def test_render_sizes_for_the_kindle_panel():
    now = dt.datetime(2026, 9, 26, 9, 0)
    img = board.render(board.queue_items(QUEUE, TODAY),
                       [{"title": "Box 84°F", "sub": "in band", "level": 0}], now)
    assert img.size == board.SIZE
    kindle = board.for_device(img, "kindle")
    assert kindle.size == (758, 1024)
    assert {v for _, v in kindle.getcolors(256)} <= {v * 17 for v in range(16)}


def test_close_queue_row_moves_it_to_shipped_in_the_shipped_shape(tmp_path):
    q = tmp_path / "queue.md"
    q.write_text(QUEUE)
    row = next(i for i in board.queue_items(QUEUE, TODAY) if i["title"] == "Check the marigold seed bag")
    assert board.close_queue_row(row["line"], TODAY, q) == "Check the marigold seed bag"
    text = q.read_text()
    assert row["line"] not in text
    shipped = text.split("## Shipped")[1]
    assert "| 2026-09-26 | garden | Check the marigold seed bag (closed from the board) |" in shipped
    # a stale frame can't close a row that already moved
    try:
        board.close_queue_row(row["line"], TODAY, q)
        raise AssertionError("closed a row that was gone")
    except ValueError:
        pass


def test_memory_column_appears_only_with_proposals_and_buttons_hit_first():
    now = dt.datetime(2026, 9, 26, 9, 0)
    mem = [{"title": "Bodhi is the sire", "sub": "episodic", "level": 1, "key": "memory:x",
            "memory": "x", "detail": "Bodhi is the sire.", "choices": board.MEMORY_CHOICES}]
    hits = []
    board.render([], [], now, hits=hits)
    assert hits == []
    board.render([], [], now, selected="memory:x", note="Bodhi is the sire.", hits=hits, memory=mem)
    assert [h["item"]["key"] for h in hits[:2]] == ["choice:promote", "choice:discard"]
    assert hits[2]["item"]["key"] == "memory:x"


def test_old_rows_ask_keep_done_trash_and_keep_snoozes(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    q.write_text(QUEUE)
    monkeypatch.setattr(board, "BOARD_QUEUE", q)
    monkeypatch.setattr(board.config, "BOARD_REVIEW_STATE", tmp_path / "review.json")
    items = {i["title"]: i for i in board.read_queue(TODAY)}
    old, young = items["Tailscale ACL restricting port 8730"], items["Check the marigold seed bag"]
    assert old["choices"] == board.QUEUE_CHOICES and "choices" not in young

    now = dt.datetime(2026, 9, 26, 9, 0)
    assert board.complete(old, "keep", now).startswith("Kept")
    assert "choices" not in {i["title"]: i for i in board.read_queue(TODAY)}["Tailscale ACL restricting port 8730"]
    later = TODAY + dt.timedelta(days=board.REVIEW_DAYS)
    assert {i["title"]: i for i in board.read_queue(later)}["Tailscale ACL restricting port 8730"]["choices"]


def test_trash_deletes_without_a_shipped_entry(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    q.write_text(QUEUE)
    monkeypatch.setattr(board, "BOARD_QUEUE", q)
    monkeypatch.setattr(board.config, "BOARD_REVIEW_STATE", tmp_path / "review.json")
    monkeypatch.setattr(board, "TRASH_LOG", tmp_path / "trashed.tsv")
    old = {i["title"]: i for i in board.read_queue(TODAY)}["Tailscale ACL restricting port 8730"]
    board.complete(old, "trash", dt.datetime(2026, 9, 26, 9, 0))
    assert old["line"] in (tmp_path / "trashed.tsv").read_text()  # a misfire can be put back
    text = q.read_text()
    assert old["line"] not in text and "Tailscale" not in text.split("## Shipped")[1]


def test_queue_page_is_read_only_and_escapes(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    q.write_text(QUEUE.replace("pot the rosemary", "pot <b>rosemary</b>"))
    monkeypatch.setattr(board, "BOARD_QUEUE", q)
    monkeypatch.setattr(board.config, "BOARD_REVIEW_STATE", tmp_path / "review.json")
    monkeypatch.setattr(board, "home_items", lambda now: [{"title": "Box <hot>", "sub": "day 5", "level": 2}])
    monkeypatch.setattr(board, "read_memory", lambda: [])
    page = board.queue_page(dt.datetime(2026, 9, 26, 9, 0).astimezone())
    assert "Box &lt;hot&gt;" in page and "<form" not in page and "<button" not in page
    assert "MEMORY" not in page  # empty inbox, no section
    snap = board.snapshot(dt.datetime(2026, 9, 26, 9, 0).astimezone())
    assert [r["title"] for r in snap["columns"]["screen"]][:1] == ["Tailscale ACL restricting port 8730"]


def test_ids_are_stable_across_edits_to_the_rest_of_the_row():
    a = board.queue_items(QUEUE, TODAY)
    edited = QUEUE.replace("for the type", "for the type and the sprout days")
    b = board.queue_items(edited, TODAY)
    ids = lambda rows: {r["title"]: r["id"] for r in rows}
    assert ids(a) == ids(b)
    assert len(set(ids(a).values())) == len(a)


def test_act_on_queue_by_id_and_status(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    q.write_text(QUEUE)
    monkeypatch.setattr(board, "BOARD_QUEUE", q)
    monkeypatch.setattr(board.config, "BOARD_REVIEW_STATE", tmp_path / "review.json")
    now = dt.datetime(2026, 9, 26, 9, 0)
    rows = {r["title"]: r for r in board.read_queue(TODAY)}
    assert rows["Tailscale ACL restricting port 8730"]["status"] == "review"
    assert rows["Check the marigold seed bag"]["status"] == "open"
    assert board.act_on_queue(rows["Check the marigold seed bag"]["id"], "done", now).startswith("Done")
    try:
        board.act_on_queue(rows["Check the marigold seed bag"]["id"], "done", now)
        raise AssertionError("closed twice")
    except LookupError:
        pass
    try:
        board.act_on_queue(rows["Tailscale ACL restricting port 8730"]["id"], "delete", now)
        raise AssertionError("accepted an unknown action")
    except ValueError:
        pass


def test_page_shows_done_buttons_only_with_a_token(tmp_path, monkeypatch):
    q = tmp_path / "queue.md"
    q.write_text(QUEUE)
    monkeypatch.setattr(board, "BOARD_QUEUE", q)
    monkeypatch.setattr(board.config, "BOARD_REVIEW_STATE", tmp_path / "review.json")
    monkeypatch.setattr(board, "home_items", lambda now: [])
    monkeypatch.setattr(board, "read_memory", lambda: [])
    now = dt.datetime(2026, 9, 26, 9, 0).astimezone()
    assert "<button" not in board.queue_page(now)
    assert board.queue_page(now, token="t").count("<button") == 3
