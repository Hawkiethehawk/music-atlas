/** Verify a generated payload on an isolated, headless browser; never publish it. */
import assert from 'node:assert/strict';
import path from 'node:path';
import {readFile, writeFile, copyFile} from 'node:fs/promises';
import {chromium} from '../web/node_modules/playwright/index.mjs';
import {createIsolatedServer, PROJECT_ROOT} from '../web/tests/helpers.mjs';

const directory=path.resolve(process.argv[2] || 'runtime/agent-copy-122-run');
const payload=JSON.parse(await readFile(path.join(directory,'web_payload.json'),'utf8'));
assert.equal(payload.interests.length,3);
assert.equal(payload.recommendations.length,10);
assert.equal(new Set(payload.recommendations.map(r=>r.why)).size,10);
const forbidden=/last[\s.\-]*fm|audioscrobbler|力量感|旋律感|氛围感|压抑|孤独|痛苦|负面/i;
assert.ok(!forbidden.test(payload.issue.lede));
const server=await createIsolatedServer({label:'agent-copy-acceptance'});
let browser;
const checked=[];
try {
    await copyFile(path.join(directory,'web_payload.json'),path.join(server.runtimeDir,'current.json'));
    browser=await chromium.launch({headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1100}});
    const errors=[];
    page.on('pageerror',error=>errors.push(String(error)));
    // These are UI checks; preserve retrieved covers and avoid fresh metadata searches.
    await page.route('**/api/meta?**',route=>route.fulfill({json:{cover:null,links:{}}}));
    await page.route(/https:\/\//,route=>route.fulfill({status:200,contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="600" height="600" fill="#29221b"/><text x="300" y="300" fill="#dbd0b8" font-size="24" text-anchor="middle">Cover omitted in copy acceptance</text></svg>'}));
    async function visit(hash) {
        await page.goto(server.baseUrl+'/'+hash);
        await page.waitForFunction(()=>document.body.classList.contains('atlas-ready'));
        const text=await page.locator('body').innerText();
        assert.ok(!/last[\s.\-]*fm|audioscrobbler/i.test(text),hash+' exposes provider branding');
        assert.equal(await page.locator('.empty-state').filter({hasText:'页面不存在'}).count(),0);
        checked.push(hash);
    }
    await visit('#/discover');
    assert.equal(await page.locator('.hero .lede').innerText(),payload.issue.lede);
    assert.equal(await page.locator('.trow').count(),10);
    await page.screenshot({path:path.join(directory,'home.png'),fullPage:true});
    for(const rec of payload.recommendations) {
        for(const route of ['why','track']) {
            await visit('#/'+route+'/'+encodeURIComponent(rec.id));
            assert.equal(await page.locator('.detail-note').count(),4);
            const text=await page.locator('.dgrid').innerText();
            for(const value of Object.values(rec.details))assert.ok(text.includes(value));
            assert.ok(!/·\s*—/.test(await page.locator('.dsub').innerText()));
            if(route==='why'&&rec.rank===1)await page.screenshot({path:path.join(directory,'detail.png'),fullPage:true});
        }
        for(const [route,value] of [['album',rec.album],['artist',rec.artist]]) {
            await visit('#/'+route+'/'+encodeURIComponent(value));
            assert.equal(await page.locator('.dgrid').count(),1);
            assert.ok(!/·\s*$/.test(await page.locator('.dsub').innerText()));
        }
    }
    await visit('#/taste');
    assert.equal(await page.locator('.island').count(),3);
    await page.screenshot({path:path.join(directory,'islands.png'),fullPage:true});
    for(const island of payload.interests) {
        await visit('#/interest/'+encodeURIComponent(island.id));
        for(const details of await page.locator('details').all())await details.evaluate(node=>node.open=true);
        assert.ok(!/last[\s.\-]*fm/i.test(await page.locator('body').innerText()));
    }
    await visit('#/atlas');await visit('#/sources');
    await page.setViewportSize({width:390,height:844});
    await visit('#/discover');
    assert.equal(await page.locator('.hero .lede').innerText(),payload.issue.lede);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    assert.deepEqual(errors,[]);
    const acceptance={status:'accepted',headless:true,source_tracks:payload.source.trackCount,
        recommendations:10,islands:3,checked_routes:checked,script_errors:errors,provider_brand_visible:false,
        cover_images:'stubbed for copy acceptance; image loading is outside this check'};
    await writeFile(path.join(directory,'browser_acceptance.json'),JSON.stringify(acceptance,null,2));
    // A portable preview uses the same SPA template and the validated, captured payload.
    let html=await readFile(path.join(PROJECT_ROOT,'web','editorial-atlas.html'),'utf8');
    const data=JSON.stringify({ok:true,...payload}).replace(/</g,'\\u003c');
    html=html.replace('async function bootAtlas(){','async function bootAtlas(){ applyAtlasPayload('+data+');document.body.classList.add("atlas-ready");render();return;');
    await writeFile(path.join(directory,'preview.html'),html);
    console.log(JSON.stringify({status:'accepted',routes:checked.length,directory}));
} finally {
    if(browser)await browser.close();
    await server.stop();
}
