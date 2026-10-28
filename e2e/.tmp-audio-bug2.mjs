import { chromium } from 'playwright';
const base='http://127.0.0.1:8136';
const browser=await chromium.launch(); const page=await browser.newPage({viewport:{width:1680,height:1000}});
const errors=[]; page.on('pageerror',e=>errors.push(String(e).slice(0,300)));
await page.goto(base+'/login',{waitUntil:'networkidle'}); await page.fill('input#username, input[type="text"]','admin'); await page.fill('input[type="password"]','admin123'); await page.click('button[type="submit"]'); await page.waitForURL(u=>!u.pathname.includes('login'),{timeout:20000}).catch(()=>{});
for (const path of ['/r/1','/r/2']) {
  await page.goto(base+path,{waitUntil:'networkidle'}); await page.waitForTimeout(1800);
  const info=await page.evaluate(()=>({text:document.body.innerText.slice(0,100), audio:document.querySelectorAll('audio').length, video:document.querySelectorAll('video').length, title:document.title}));
  console.log(path,JSON.stringify(info),'errors',errors.length,JSON.stringify(errors.slice(-2)));
  await page.screenshot({path:'/home/wpcwzy/echoic/.cache/audio-fixed'+path.replace(/\//g,'-')+'.png'});
}
await browser.close();
