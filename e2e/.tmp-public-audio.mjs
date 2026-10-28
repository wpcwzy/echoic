import { chromium } from 'playwright'; import { readFileSync } from 'node:fs';
const token=readFileSync('/home/wpcwzy/echoic/.cache/audio-share-token.txt','utf8').trim();
const browser=await chromium.launch(); const page=await browser.newPage({viewport:{width:1280,height:900}});
const errors=[]; page.on('pageerror',e=>errors.push(String(e).slice(0,200)));
await page.goto('http://127.0.0.1:8136/s/'+token,{waitUntil:'networkidle'}); await page.waitForTimeout(1500);
console.log('text',(await page.locator('body').innerText()).slice(0,80).replace(/\n/g,'|'),'audio',await page.locator('audio').count(),'errors',errors);
await page.screenshot({path:'/home/wpcwzy/echoic/.cache/public-audio-fixed.png'});
await browser.close();
