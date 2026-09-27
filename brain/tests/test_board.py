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
