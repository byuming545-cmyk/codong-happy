"""Combat acceptance tests against the actual browser game.

Run a local HTTP server, then: python3 tests/test_combat.py
Set GAME_TEST_URL to test another server. No observation API is shipped to users:
this script injects its accessors into the served HTML only for this browser.
"""
import json
import math
import os
import shutil

from playwright.sync_api import sync_playwright


BASE_URL = os.environ.get("GAME_TEST_URL", "http://127.0.0.1:8000/")
OBSERVATION = """
window.__combatTest = {
  get hero(){return hero}, get enemies(){return enemies},
  get bullets(){return bullets}, get enemyBullets(){return enemyBullets},
  get score(){return score}, get state(){return state},
  get gameTime(){return gameTime}, set gameTime(value){gameTime=value},
  get overloadUntil(){return overloadUntil}, get overloadReadyAt(){return overloadReadyAt},
  get definitions(){return ENEMY_TYPES}, get tierSprites(){return tierSprites},
  startGame, updateGame, spawnEnemy, selectEnemyType, fire,
  activateOverload, enemyFire, damageHero, killEnemy
};
"""


def instrument(page):
    def route_document(route):
        response = route.fetch()
        html = response.text()
        marker = "// TEST_OBSERVATION_POINT"
        assert html.count(marker) == 1, "Missing or duplicate browser observation marker"
        route.fulfill(response=response, body=html.replace(marker, OBSERVATION + marker))

    page.route(BASE_URL, route_document)


def reset(page):
    page.evaluate("__combatTest.startGame()")
    assert page.evaluate("__combatTest.state") == "playing"


def probability_checks(page):
    result = page.evaluate("""() => {
      const t=__combatTest, counts={normal:0,advanced:0,boss:0};
      for(let i=0;i<1000;i++)counts[t.selectEnemyType((i+.5)/1000)]++;
      return {counts, boundaries:[0,.799999,.8,.979999,.98,.999999].map(t.selectEnemyType),
        definitions:Object.fromEntries(Object.entries(t.definitions).map(([k,v])=>[k,{hp:v.hp,weight:v.weight}]))};
    }""")
    assert result["counts"] == {"normal": 800, "advanced": 180, "boss": 20}, result
    assert result["boundaries"] == ["normal", "normal", "advanced", "advanced", "boss", "boss"], result
    assert result["definitions"] == {
        "normal": {"hp": 3, "weight": 80},
        "advanced": {"hp": 10, "weight": 18},
        "boss": {"hp": 100, "weight": 2},
    }, result
    return result["counts"]


def enemy_hitpoint_checks(page):
    results = page.evaluate("""() => {
      const t=__combatTest, results={};
      for(const [type,requiredHits] of [['normal',3],['advanced',10],['boss',100]]){
        t.startGame();
        const enemy=t.spawnEnemy(type);
        enemy.x=160;enemy.y=180;enemy.speed=0;
        // Settled Boss position is deliberate; this isolates damage from movement.
        if(type==='boss')enemy.targetY=180;
        const initial={hp:enemy.hp,maxHp:enemy.maxHp};
        let earlyDeath=false,wrongDamage=false,scoreBeforeLast=0;
        for(let hit=1;hit<=requiredHits;hit++){
          t.bullets.splice(0);
          t.bullets.push({x:enemy.x+enemy.w/2-4,y:enemy.y+enemy.h/2-10,
            w:8,h:20,vx:0,vy:0,dead:false});
          t.updateGame(0);
          if(hit<requiredHits){
            earlyDeath ||= enemy.dead;
            wrongDamage ||= enemy.hp!==requiredHits-hit;
            scoreBeforeLast=t.score;
          }
        }
        results[type]={initial,earlyDeath,wrongDamage,scoreBeforeLast,
          hp:enemy.hp,dead:enemy.dead,score:t.score};
      }
      // One projectile intersecting two ships must inflict exactly one damage.
      t.startGame();
      const a=t.spawnEnemy('normal'),b=t.spawnEnemy('normal');
      for(const e of [a,b]){e.x=160;e.y=180;e.speed=0;}
      t.bullets.splice(0);
      const shot={x:175,y:190,w:8,h:20,vx:0,vy:0,dead:false};
      t.bullets.push(shot);
      t.updateGame(0);
      results.singleHit={hp:[a.hp,b.hp].sort(),spent:shot.dead,
        alive:[a,b].filter(e=>!e.dead).length,score:t.score};
      return results;
    }""")
    for tier, hp in [("normal", 3), ("advanced", 10), ("boss", 100)]:
        result = results[tier]
        assert result["initial"] == {"hp": hp, "maxHp": hp}, (tier, result)
        assert not result["earlyDeath"] and not result["wrongDamage"], (tier, result)
        assert result["scoreBeforeLast"] == 0, (tier, result)
        assert result["hp"] == 0 and result["dead"], (tier, result)
        assert result["score"] > 0, (tier, result)
    assert results["singleHit"] == {"hp": [2, 3], "spent": True, "alive": 2, "score": 0}, results
    return {tier: result["initial"]["hp"] for tier, result in results.items() if tier != "singleHit"}


def hero_damage_checks(page):
    result = page.evaluate("""() => {
      const t=__combatTest;
      t.startGame();
      const initial={hp:t.hero.hp,maxHp:t.hero.maxHp};
      let earlyDeath=false,wrongDamage=false;
      for(let hit=1;hit<=20;hit++){
        t.bullets.splice(0);t.enemies.splice(0);t.enemyBullets.splice(0);
        const h=t.hero;
        t.enemyBullets.push({x:h.x+h.w/2-4,y:h.y+h.h/2-8,w:8,h:16,
          vx:0,vy:0,dead:false,kind:'advanced'});
        t.updateGame(0);
        wrongDamage ||= t.hero.hp!==20-hit;
        if(hit<20)earlyDeath ||= t.state!=='playing';
      }
      const bullets={hp:t.hero.hp,state:t.state,earlyDeath,wrongDamage};
      const collisions={};
      for(const type of ['normal','advanced','boss']){
        t.startGame();
        const h=t.hero,e=t.spawnEnemy(type);
        e.x=h.x;e.y=h.y;e.speed=0;
        if(type==='boss')e.targetY=h.y;
        t.updateGame(0);
        collisions[type]={state:t.state,hp:t.hero.hp};
      }
      t.startGame();t.damageHero(25);
      return {initial,bullets,collisions,overkill:{hp:t.hero.hp,state:t.state}};
    }""")
    assert result["initial"] == {"hp": 20, "maxHp": 20}, result
    assert result["bullets"] == {"hp": 0, "state": "over", "earlyDeath": False, "wrongDamage": False}, result
    assert all(value["state"] == "over" for value in result["collisions"].values()), result
    assert result["overkill"] == {"hp": 0, "state": "over"}, result
    return result


def shot_geometry_checks(page):
    result = page.evaluate("""() => {
      const t=__combatTest;
      function velocities(enemy,mode){
        t.enemyBullets.splice(0);t.enemyFire(enemy,mode);
        return t.enemyBullets.map(b=>({vx:b.vx,vy:b.vy,kind:b.kind}));
      }
      t.startGame();
      const advanced=t.spawnEnemy('advanced');advanced.y=180;
      const advancedShots=velocities(advanced,'fan');
      t.enemies.splice(0);
      const boss=t.spawnEnemy('boss');boss.y=boss.targetY;
      const bossShots=velocities(boss,'fan'),sweep=[];
      for(const degree of [-70,0,70]){
        boss.sweepAngle=degree*Math.PI/180;
        sweep.push({degree,shots:velocities(boss,'sweep')});
      }
      return {advanced:advancedShots,boss:bossShots,sweep};
    }""")

    def check(shots, angles, speed):
        actual = sorted(math.degrees(math.atan2(shot["vx"], shot["vy"])) for shot in shots)
        assert len(actual) == len(angles), (shots, angles)
        assert all(abs(a - b) < 0.01 for a, b in zip(actual, angles)), (actual, angles)
        assert all(abs(math.hypot(shot["vx"], shot["vy"]) - speed) < 0.01 for shot in shots), shots

    check(result["advanced"], [-30, 30], 150)
    check(result["boss"], [-50, -25, 0, 25, 50], 220)
    for sweep in result["sweep"]:
        check(sweep["shots"], [sweep["degree"]], 330)
    return {"advanced_angles": [-30, 30], "boss_angles": [-50, -25, 0, 25, 50], "sweep_range": [-70, 70]}


def cadence_checks(page):
    result = page.evaluate("""() => {
      const t=__combatTest, result={};
      for(const [type,duration] of [['normal',7],['advanced',7],['boss',19]]){
        t.startGame();
        const target=t.spawnEnemy(type);
        target.x=100;target.y=type==='boss'?target.targetY:180;target.speed=0;
        target.age=0;target.fireTimer=0;target.sweepTimer=0;
        const events=[];
        for(let tick=0;tick<duration*100;tick++){
          t.bullets.splice(0);t.enemyBullets.splice(0);
          t.enemies.splice(0,t.enemies.length,...t.enemies.filter(e=>e===target));
          t.updateGame(.01);
          for(const kind of ['advanced','boss','sweep']){
            const shots=t.enemyBullets.filter(b=>b.kind===kind);
            if(shots.length)events.push({time:t.gameTime,age:target.age,kind,count:shots.length,
              angles:shots.map(b=>Math.atan2(b.vx,b.vy)*180/Math.PI)});
          }
        }
        result[type]={events,state:t.state,y:target.y,targetY:target.targetY};
      }
      return result;
    }""")
    assert result["normal"]["events"] == [], result["normal"]
    advanced = result["advanced"]["events"]
    assert len(advanced) >= 3, advanced
    assert all(event["kind"] == "advanced" and event["count"] == 2 for event in advanced), advanced
    assert all(abs(b["time"] - a["time"] - 2.2) < 0.025 for a, b in zip(advanced, advanced[1:])), advanced
    boss = result["boss"]
    assert abs(boss["y"] - boss["targetY"]) < 0.01 and boss["state"] == "playing", boss
    fan = [event for event in boss["events"] if event["kind"] == "boss"]
    sweep = [event for event in boss["events"] if event["kind"] == "sweep"]
    assert len(fan) >= 12 and all(event["count"] == 5 for event in fan), fan
    fan_intervals = [b["time"] - a["time"] for a, b in zip(fan, fan[1:])]
    regular = [interval for interval in fan_intervals if interval < 1]
    assert regular and all(abs(interval - 0.75) < 0.025 for interval in regular), fan
    groups = []
    for event in sweep:
        assert event["count"] == 1, event
        if not groups or event["time"] - groups[-1][-1]["time"] > 0.2:
            groups.append([])
        groups[-1].append(event)
    assert len(groups) == 2, groups
    for group in groups:
        assert 23 <= len(group) <= 25, group
        assert 2.0 <= group[-1]["time"] - group[0]["time"] <= 2.21, group
        assert all(abs(b["time"] - a["time"] - .09) < .021 for a, b in zip(group, group[1:])), group
        angles = [event["angles"][0] for event in group]
        assert min(angles) >= -70.1 and max(angles) <= 70.1, angles
        assert min(angles) < -60 and max(angles) > 60, angles
        assert angles == sorted(angles), angles
    assert abs(groups[1][0]["time"] - groups[0][0]["time"] - 9) < .03, groups
    return {"advanced_interval": 2.2, "boss_fan_interval": .75,
            "boss_sweep_interval": .09, "sweep_bursts": [len(group) for group in groups]}


def boss_spawn_checks(page):
    result = page.evaluate("""() => {
      const t=__combatTest;
      t.startGame();
      const first=t.spawnEnemy('boss'),duplicate=t.spawnEnemy('boss');
      const oneAlive=t.enemies.filter(e=>e.type==='boss'&&!e.dead).length;
      first.y=first.targetY-1;
      t.bullets.splice(0);t.updateGame(.1);
      const arrival={y:first.y,targetY:first.targetY};
      for(let i=0;i<20;i++){t.bullets.splice(0);t.enemyBullets.splice(0);t.updateGame(.05);}
      const heldY=first.y;
      t.killEnemy(first);
      const replacement=t.spawnEnemy('boss');
      // Force the rare end of the random distribution, without changing its weights.
      t.startGame();
      const originalRandom=Math.random;
      try{
        Math.random=()=>.999;
        t.gameTime=29.99;const early=t.spawnEnemy();
        t.enemies.splice(0);
        t.gameTime=30;const eligible=t.spawnEnemy();
        const automaticDuplicate=t.spawnEnemy();
        if(eligible&&eligible.type==='boss')t.killEnemy(eligible);
        t.gameTime=89.99;const cooldown=t.spawnEnemy();
        t.gameTime=90;const ready=t.spawnEnemy();
        return {firstType:first&&first.type,duplicate:duplicate===null,oneAlive,arrival,heldY,
          replacementType:replacement&&replacement.type,early:early&&early.type,
          eligible:eligible&&eligible.type,automaticDuplicate:automaticDuplicate&&automaticDuplicate.type,
          cooldown:cooldown&&cooldown.type,ready:ready&&ready.type};
      }finally{Math.random=originalRandom;}
    }""")
    assert result["firstType"] == "boss" and result["duplicate"] and result["oneAlive"] == 1, result
    assert abs(result["arrival"]["y"] - result["arrival"]["targetY"]) < .001, result
    assert abs(result["heldY"] - result["arrival"]["targetY"]) < .001, result
    assert result["replacementType"] == "boss", result
    assert result["early"] != "boss" and result["eligible"] == "boss", result
    assert result["automaticDuplicate"] != "boss" and result["cooldown"] != "boss", result
    assert result["ready"] == "boss", result
    return result


def overload_timing_checks(page):
    result = page.evaluate("""() => {
      const t=__combatTest;
      t.startGame();t.gameTime=10;t.activateOverload();
      const first={until:t.overloadUntil,ready:t.overloadReadyAt};
      function lanes(){
        t.bullets.splice(0);t.fire();
        return t.bullets.map(b=>({x:b.x,vx:b.vx||0,vy:b.vy}));
      }
      const active=lanes();
      t.gameTime=10.5;t.activateOverload();
      const repeated={until:t.overloadUntil,ready:t.overloadReadyAt};
      t.gameTime=19.999;const beforeExpiry=lanes();
      t.gameTime=20;const expired=lanes();
      t.gameTime=69.999;t.activateOverload();
      const cooling={until:t.overloadUntil,ready:t.overloadReadyAt};
      t.gameTime=70;t.activateOverload();
      const ready={until:t.overloadUntil,ready:t.overloadReadyAt,lanes:lanes().length};
      return {first,active,repeated,beforeExpiry:beforeExpiry.length,expired:expired.length,cooling,ready};
    }""")
    assert result["first"] == {"until": 20, "ready": 70}, result
    assert result["repeated"] == result["first"] and result["cooling"] == result["first"], result
    assert len(result["active"]) == 3 and result["beforeExpiry"] == 3 and result["expired"] == 1, result
    assert len({shot["x"] for shot in result["active"]}) == 3, result
    assert all(shot["vx"] == 0 and shot["vy"] < 0 for shot in result["active"]), result
    assert result["ready"] == {"until": 80, "ready": 130, "lanes": 3}, result
    return {"active_seconds": 10, "cooldown_from_activation": 60, "parallel_lanes": 3}


def input_pause_restart_checks(page):
    reset(page)
    page.evaluate("""() => {
      window.__contextEvents=[];
      document.addEventListener('contextmenu',event=>__contextEvents.push(event.defaultPrevented));
    }""")
    page.mouse.click(600, 600, button="right")
    active = page.evaluate("({time:__combatTest.gameTime,until:__combatTest.overloadUntil,ready:__combatTest.overloadReadyAt,menus:__contextEvents})")
    assert active["until"] > active["time"], active
    assert abs(active["ready"] - active["until"] - 50) < .001, active
    assert active["menus"] and all(active["menus"]), active
    page.locator("#pause").click()
    page.wait_for_function("__combatTest.state==='paused'")
    snapshot = "({time:__combatTest.gameTime,until:__combatTest.overloadUntil,ready:__combatTest.overloadReadyAt,hp:__combatTest.hero.hp})"
    paused = page.evaluate(snapshot)
    page.wait_for_timeout(300)
    assert page.evaluate(snapshot) == paused, (paused, page.evaluate(snapshot))
    page.locator("#pause").click()
    page.wait_for_function("__combatTest.state==='playing'")
    page.evaluate("""() => {
      const t=__combatTest;
      t.spawnEnemy('normal');
      t.enemyBullets.push({x:50,y:50,w:8,h:16,vx:0,vy:0,dead:false,kind:'advanced'});
      window.__stalePlayerBullet={x:50,y:50,w:5,h:25,vx:0,vy:0,dead:false};
      t.bullets.push(__stalePlayerBullet);
      t.damageHero(20);
    }""")
    page.wait_for_function("__combatTest.state==='over'")
    page.locator("#restart").click()
    page.wait_for_function("__combatTest.state==='playing'")
    restarted = page.evaluate("""() => {
      const t=__combatTest;
      return {hp:t.hero.hp,score:t.score,enemies:t.enemies.length,enemyBullets:t.enemyBullets.length,
        until:t.overloadUntil,ready:t.overloadReadyAt,time:t.gameTime,
        staleBullet:t.bullets.includes(__stalePlayerBullet)};
    }""")
    assert restarted["hp"] == 20 and restarted["score"] == 0, restarted
    assert restarted["enemies"] == 0 and restarted["enemyBullets"] == 0, restarted
    assert restarted["until"] == 0 and restarted["ready"] == 0 and restarted["time"] < .5, restarted
    assert not restarted["staleBullet"], restarted
    # The same visible control serves touch devices without a right mouse button.
    page.locator("#overload").click()
    assert page.evaluate("__combatTest.overloadUntil>__combatTest.gameTime")
    return {"right_click": "activated; browser menu suppressed", "pause": "timers frozen", "restart": "cleared"}


def mobile_overload_check(browser, errors, external):
    context = browser.new_context(viewport={"width": 390, "height": 844},
                                  device_scale_factor=2, is_mobile=True, has_touch=True)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("request", lambda request: external.add(request.url)
            if not request.url.startswith((BASE_URL, "data:", "blob:")) else None)
    instrument(page)
    page.goto(BASE_URL, wait_until="networkidle")
    page.wait_for_function("window.__combatTest&&__combatTest.state!=='loading'")
    reset(page)
    control = page.locator("#overload").bounding_box()
    assert control, "Touch overload button is not visible"
    page.touchscreen.tap(control["x"] + control["width"] / 2, control["y"] + control["height"] / 2)
    page.wait_for_function("__combatTest.overloadUntil>__combatTest.gameTime")
    context.close()
    return "touch activation passed at 390x844"


def main():
    errors, external = [], set()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=shutil.which("chromium"), headless=True,
                                             args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("request", lambda request: external.add(request.url)
                if not request.url.startswith((BASE_URL, "data:", "blob:")) else None)
        instrument(page)
        page.goto(BASE_URL, wait_until="networkidle")
        page.wait_for_function("window.__combatTest&&__combatTest.state!=='loading'")
        results = {}
        for name, check in [
            ("tier_probabilities", probability_checks),
            ("enemy_hitpoints", enemy_hitpoint_checks),
            ("hero_damage", hero_damage_checks),
            ("shot_geometry", shot_geometry_checks),
            ("fire_cadence", cadence_checks),
            ("boss_spawning", boss_spawn_checks),
            ("overload", overload_timing_checks),
            ("input_pause_restart", input_pause_restart_checks),
        ]:
            results[name] = check(page)
            print(f"PASS {name}", flush=True)
        context.close()
        results["mobile_overload"] = mobile_overload_check(browser, errors, external)
        print("PASS mobile_overload", flush=True)
        browser.close()
    assert not errors, errors
    assert not external, sorted(external)
    results.update({"browser_errors": errors, "external_requests": sorted(external)})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
