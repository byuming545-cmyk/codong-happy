"""Browser acceptance checks; runtime observation is injected only into this test.

Run against the local game server: python3 tests/test_game.py
"""
import json
import time
import os
import shutil
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("GAME_TEST_URL", "http://127.0.0.1:8000/")
OUTPUT = Path(tempfile.gettempdir()) / "deep-space-game-check"
OUTPUT.mkdir(parents=True, exist_ok=True)
OBSERVATION = """
window.__gameTest = {
  get hero() { return hero; },
  get bullets() { return bullets; },
  get enemies() { return enemies; },
  get enemyBullets() { return enemyBullets; },
  get shotsFired() { return shotsFired; },
  get state() { return state; },
  get score() { return score; },
  get width() { return width; },
  get height() { return height; },
  get frameCount() { return frameCount; },
  get background() { return background; },
  startGame, updateGame, spawnEnemy
};
"""


def install_observer(page):
    def instrument(route):
        response = route.fetch()
        html = response.text()
        marker = "// TEST_OBSERVATION_POINT"
        assert html.count(marker) == 1, "Application observation marker missing or duplicated"
        route.fulfill(response=response, body=html.replace(marker, OBSERVATION + marker))

    page.route(BASE_URL, instrument)


def wait_playing(page):
    page.wait_for_function("window.__gameTest && __gameTest.state !== 'loading'")
    if page.evaluate("__gameTest.state") != "playing":
        page.evaluate("__gameTest.startGame()")
    page.wait_for_function("__gameTest.state === 'playing'")


def hero_position(page):
    return page.evaluate("""() => {
      const {hero, width, height, state} = __gameTest;
      return {x: hero.x, y: hero.y, w: hero.w, h: hero.h, width, height, state};
    }""")


def assert_at_pointer(page, x, y):
    position = hero_position(page)
    expected_x = max(0, min(position["width"] - position["w"], x - position["w"] / 2))
    expected_y = max(0, min(position["height"] - position["h"], y - position["h"] / 2))
    assert abs(position["x"] - expected_x) < 0.75, (x, y, position, expected_x)
    assert abs(position["y"] - expected_y) < 0.75, (x, y, position, expected_y)
    assert position["state"] == "playing", position


def assert_viewport(page, viewport, dpr):
    page.wait_for_function(
        "([w,h]) => __gameTest.width === w && __gameTest.height === h",
        arg=[viewport["width"], viewport["height"]],
    )
    canvas = page.locator("canvas#game")
    dimensions = canvas.evaluate("""c => ({
      width: c.width, height: c.height,
      cssWidth: c.getBoundingClientRect().width,
      cssHeight: c.getBoundingClientRect().height,
      overflowX: document.documentElement.scrollWidth - innerWidth,
      overflowY: document.documentElement.scrollHeight - innerHeight
    })""")
    assert abs(dimensions["cssWidth"] - viewport["width"]) <= 1, dimensions
    assert abs(dimensions["cssHeight"] - viewport["height"]) <= 1, dimensions
    assert dimensions["width"] == round(viewport["width"] * min(dpr, 2)), dimensions
    assert dimensions["height"] == round(viewport["height"] * min(dpr, 2)), dimensions
    assert dimensions["overflowX"] <= 1 and dimensions["overflowY"] <= 1, dimensions


def desktop_checks(browser, errors, requests):
    viewport = {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, device_scale_factor=1)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("request", lambda request: requests.add(request.url))
    install_observer(page)
    page.goto(BASE_URL, wait_until="networkidle")
    wait_playing(page)
    assert_viewport(page, viewport, 1)

    # Hover without pressing must immediately follow both axes, including edges.
    for x, y in [(720, 450), (115, 170), (1280, 740), (0, 450),
                 (1439, 450), (720, 0), (720, 899), (2, 2), (1438, 898)]:
        page.evaluate("__gameTest.enemies.splice(0); __gameTest.enemyBullets.splice(0)")
        page.mouse.move(x, y)
        page.wait_for_timeout(25)
        assert_at_pointer(page, x, y)

    # Resize exercises CSS coordinates separately from canvas backing pixels.
    resized = {"width": 1100, "height": 740}
    page.set_viewport_size(resized)
    assert_viewport(page, resized, 1)
    page.mouse.move(550, 370)
    assert_at_pointer(page, 550, 370)
    page.set_viewport_size(viewport)
    assert_viewport(page, viewport, 1)

    page.evaluate("__gameTest.startGame()")
    start_shots = page.evaluate("__gameTest.shotsFired")
    start_time = time.monotonic()
    page.wait_for_timeout(1100)
    shots = page.evaluate("__gameTest.shotsFired") - start_shots
    elapsed = time.monotonic() - start_time
    shots_per_second = shots / elapsed
    assert 10 <= shots_per_second <= 15, {"shots": shots, "seconds": elapsed, "rate": shots_per_second}
    assert page.evaluate("__gameTest.state") == "playing"

    # One shot damages exactly one of two overlapping three-hit enemies.
    collision = page.evaluate("""() => {
      const t = __gameTest;
      t.enemies.splice(0);
      t.bullets.splice(0);
      t.enemyBullets.splice(0);
      const priorScore = t.score;
      for (let i = 0; i < 2; i++) {
        const enemy = t.spawnEnemy('normal');
        Object.assign(enemy, {x: 160, y: 160, w: 40, h: 40,
                             speed: 0, fireTimer: 1000, age: 0});
      }
      t.bullets.push({x: 175, y: 170, w: 8, h: 20, vy: 0, damage: 1, dead: false});
      t.updateGame(0);
      return {
        remainingEnemies: t.enemies.filter(enemy => !enemy.dead).length,
        hitPoints: t.enemies.map(enemy => enemy.hp).sort((a,b) => a-b),
        remainingBullets: t.bullets.filter(bullet => !bullet.dead).length,
        scoreDelta: t.score - priorScore
      };
    }""")
    assert collision["remainingEnemies"] == 2, collision
    assert collision["hitPoints"] == [2, 3], collision
    assert collision["remainingBullets"] == 0, collision
    assert collision["scoreDelta"] == 0, collision
    page.evaluate("__gameTest.enemies.splice(0); __gameTest.enemyBullets.splice(0)")

    # Both the visible control and keyboard must pause simulation and firing.
    page.locator("#pause").click()
    page.wait_for_function("__gameTest.state === 'paused'")
    paused_snapshot = """({
      shots: __gameTest.shotsFired,
      bullets: __gameTest.bullets.map(b => [b.x,b.y]),
      enemyBullets: __gameTest.enemyBullets.map(b => [b.x,b.y]),
      health: __gameTest.hero.hp
    })"""
    paused = page.evaluate(paused_snapshot)
    page.wait_for_timeout(300)
    assert page.evaluate(paused_snapshot) == paused
    page.keyboard.press("Space")
    page.wait_for_function("__gameTest.state === 'playing'")
    page.keyboard.press("Space")
    page.wait_for_function("__gameTest.state === 'paused'")
    page.locator("#pause").click()
    page.wait_for_function("__gameTest.state === 'playing'")

    # Physical collision is fatal even immediately after launch; restart resets it.
    page.evaluate("""() => {
      const t = __gameTest;
      t.startGame();
      const h = t.hero;
      t.enemies.splice(0);
      t.bullets.splice(0);
      const enemy = t.spawnEnemy('normal');
      Object.assign(enemy, {x:h.x, y:h.y, w:h.w, h:h.h,
                           speed:0, fireTimer:1000, age:0});
      t.updateGame(0);
    }""")
    page.wait_for_function("__gameTest.state === 'over'")
    page.locator("#restart").click()
    page.wait_for_function("__gameTest.state === 'playing'")
    assert page.evaluate("__gameTest.score") == 0
    page.mouse.move(720, 760)
    page.screenshot(path=str(OUTPUT / "improved-game-desktop.png"), full_page=True)
    context.close()
    return {"shot_rate": round(shots_per_second, 2), "collision": collision}


def mobile_checks(browser, errors, requests):
    viewport = {"width": 390, "height": 844}
    context = browser.new_context(viewport=viewport, device_scale_factor=2, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("request", lambda request: requests.add(request.url))
    install_observer(page)
    page.goto(BASE_URL, wait_until="networkidle")
    wait_playing(page)
    assert_viewport(page, viewport, 2)
    cdp = context.new_cdp_session(page)
    for event, x, y in [("touchStart", 150, 600), ("touchMove", 310, 250),
                        ("touchMove", 45, 730), ("touchMove", 195, 420)]:
        page.evaluate("__gameTest.enemies.splice(0); __gameTest.enemyBullets.splice(0)")
        cdp.send("Input.dispatchTouchEvent", {"type": event, "touchPoints": [
            {"x": x, "y": y, "radiusX": 5, "radiusY": 5, "force": 1, "id": 0}
        ]})
        # CDP acknowledges enqueuing input before Chromium dispatches its event.
        page.wait_for_function("""([x,y]) => {
          const t=__gameTest, h=t.hero;
          return Math.abs(h.x-Math.max(0,Math.min(t.width-h.w,x-h.w/2)))<.75 &&
                 Math.abs(h.y-Math.max(0,Math.min(t.height-h.h,y-h.h/2)))<.75;
        }""", arg=[x, y], timeout=1000)
        assert_at_pointer(page, x, y)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.screenshot(path=str(OUTPUT / "improved-game-mobile.png"), full_page=True)
    context.close()


def main():
    errors = []
    requests = set()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium"), headless=True,
                                             args=["--no-sandbox", "--disable-dev-shm-usage"])
        result = desktop_checks(browser, errors, requests)
        mobile_checks(browser, errors, requests)
        browser.close()
    external = [url for url in requests if not url.startswith((BASE_URL, "data:", "blob:"))]
    assert not external, external
    assert not errors, errors
    result.update({"desktop": "1440x900", "mobile": "390x844 @2x", "browser_errors": errors,
                   "external_requests": external, "screenshots": [
                       str(OUTPUT / "improved-game-desktop.png"), str(OUTPUT / "improved-game-mobile.png")]})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
