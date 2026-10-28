import { chromium } from 'playwright';
const base='http://127.0.0.1:8136';
const browser=await chromium.launch(); const page=await browser.newPage({viewport:{width:1680,height:1000}});
const errors=[]; const consoleErrors=[];
page.on('pageerror',e=>errors.push(String(e.stack||e))); page.on('console',m=>{if(m.type()==='error') consoleErrors.push(m.text())});
await page.goto(base+'/login',{waitUntil:'networkidle'}); await page.fill('input#username, input[type="text"]','admin'); await page.fill('input[type="password"]','admin123'); await page.click('button[type="submit"]'); await page.waitForURL(u=>!u.pathname.includes('login'),{timeout:20000}).catch(()=>{});
for (const path of ['/r/1','/r/2']) {
  await page.goto(base+path,{waitUntil:'networkidle'}); await page.waitForTimeout(1500);
  console.log(path,'url',page.url(),'body text',(await page.locator('body').innerText()).slice(0,120).replace(/\n/g,'|'));
  console.log('errors',JSON.stringify(errors.slice(-3)));
  console.log('console',JSON.stringify(consoleErrors.slice(-5)));
  await page.screenshot({path:'/home/wpcwzy/echoic/.cache/audio-bug'+path.replace(/\//g,'-')+'.png'});
}
await browser.close();
