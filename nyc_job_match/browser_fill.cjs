'use strict';
// Interactive assistance only. Never click the submit button or solve CAPTCHA.
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const emit=value=>process.stdout.write(JSON.stringify(value)+'\n');
function playwright(){try{return require('playwright');}catch{if(process.env.JOB_MATCH_PLAYWRIGHT_MODULE)return require(process.env.JOB_MATCH_PLAYWRIGHT_MODULE);throw new Error('Playwright unavailable');}}
function trustedPage(page,expected){
  try{const url=new URL(page.url());const path='/'+expected.board+'/jobs/'+expected.job_id;
    return url.protocol==='https:'&&!url.username&&!url.password&&['job-boards.greenhouse.io','boards.greenhouse.io'].includes(url.hostname)&&(url.pathname===path||url.pathname.startsWith(path+'/'));
  }catch{return false;}
}
async function fillFields(page,payload){
  const filled=[],missing=[];
  for(const field of payload.fields){
    if(!trustedPage(page,payload))throw new Error('Unexpected website');
    const answer=field.answer;
    if(answer===null||answer===undefined||answer===''||(Array.isArray(answer)&&!answer.length))continue;
    if(!/^[a-zA-Z0-9_\[\]-]+$/.test(field.name)||field.type==='input_hidden')continue;
    if(field.type==='input_file')continue;
    const byId=page.locator('[id="'+field.name+'"]');
    const byName=page.locator('[name="'+field.name+'"]');
    let locator=byId.count&&await byId.count()?byId.first():byName.first();
    if(!await locator.count())locator=page.getByLabel(field.label,{exact:true}).first();
    try{
      if(!await locator.count()||!await locator.isVisible()){missing.push(field.name);continue;}
      const tag=await locator.evaluate(element=>element.tagName.toLowerCase());
      if(tag==='select'){
        await locator.selectOption(Array.isArray(answer)?answer.map(String):String(answer));
      }else if(field.type==='multi_value_single_select'||field.type==='multi_value_multi_select'){
        const chosen=Array.isArray(answer)?answer:[answer];
        const labels=chosen.map(value=>(field.values||[]).find(option=>String(option.value)===String(value))?.label);
        if(labels.some(label=>!label))throw new Error('Unknown choice');
        const type=await locator.getAttribute('type');
        if(type==='checkbox'||type==='radio'){
          for(const label of labels)await page.getByLabel(label,{exact:true}).check({timeout:1500});
        }else{
          for(const label of labels){await locator.click({timeout:1500});await page.getByRole('option',{name:label,exact:true}).click({timeout:1500});}
        }
      }else{
        await locator.fill(String(answer),{timeout:1500});
      }
      filled.push(field.name);
    }catch{missing.push(field.name);}
  }
  if(payload.resume){
    if(!trustedPage(page,payload))throw new Error('Unexpected website');
    try{
      // The live hosted form's resume field is kept distinct from cover letters.
      let upload=page.locator('#resume input[type=file],input[type=file][id=resume],input[type=file][name=resume]').first();
      if(!await upload.count()){
        const section=page.locator('[data-field="resume"],.resume');
        if(await section.count())upload=section.locator('input[type=file]').first();
      }
      if(!await upload.count())throw new Error('No unambiguous resume upload');
      const extension=payload.resume.name.split('.').pop().toLowerCase();
      const mime={pdf:'application/pdf',docx:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',txt:'text/plain',md:'text/markdown'}[extension]||'application/octet-stream';
      await upload.setInputFiles({name:payload.resume.name,mimeType:mime,buffer:Buffer.from(payload.resume.base64,'base64')});
      filled.push('resume');
    }catch{missing.push('resume');}
  }
  return {filled,missing};
}
async function receipt(page,expected){
  let url;try{url=new URL(page.url());}catch{return false;}
  if(!['job-boards.greenhouse.io','boards.greenhouse.io'].includes(url.hostname))return false;
  if(url.protocol!=='https:'||url.username||url.password)return false;
  if(expected){const path='/'+expected.board+'/jobs/'+expected.job_id;if(url.pathname!==path&&!url.pathname.startsWith(path+'/'))return false;}
  const pageText=await page.locator('body').innerText().catch(()=>'');
  if(/not (?:been )?(?:submitted|received)|hasn't been submitted|application.{0,40}(?:pending|incomplete)|complete (?:the |any )?remaining|required fields/i.test(pageText))return false;
  const confirmation=page.locator('#application_confirmation');
  if(await confirmation.count()&&await confirmation.isVisible()){
    const text=(await confirmation.innerText()).trim();
    if(/(?:application.{0,60}(?:submitted|received)|received.{0,40}application)/i.test(text))return true;
  }
  return await page.getByRole('heading',{name:/^(Application submitted!?|Your application has been submitted[!.]?)$/i}).isVisible().catch(()=>false);
}
async function main(){
  const chunks=[];for await(const chunk of process.stdin)chunks.push(chunk);
  const payload=JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if(!['datadog','figma'].includes(payload.board)||!/^\d+$/.test(payload.job_id))throw new Error('Invalid posting');
  const browser=await playwright().chromium.launch({channel:'msedge',headless:false});
  const page=await browser.newPage();page.setDefaultTimeout(2000);
  let closed=false;page.on('close',()=>{closed=true;});
  const url='https://job-boards.greenhouse.io/'+payload.board+'/jobs/'+payload.job_id;
  try{
    await page.goto(url,{waitUntil:'domcontentloaded',timeout:45000});
    await page.locator('input').first().waitFor({timeout:15000});
    const result=await fillFields(page,payload);
    emit({status:'filled',...result,message:'The official form is open. Filled '+result.filled.length+' fields. Review remaining questions, attachments, and verification on the employer site. Nothing has been submitted.'});
  }catch{
    emit({status:'filled',filled:[],missing:payload.fields.map(field=>field.name).concat('resume'),message:'The official form is open, but autofill was incomplete. Complete it manually; the page may need verification or more time to load. Nothing has been submitted.'});
  }
  const end=Date.now()+30*60*1000;
  while(!closed&&Date.now()<end){
    if(await receipt(page,payload)){
      emit({status:'confirmed',receipt_url:page.url(),message:'The employer site displayed an explicit submission receipt. Your application was recorded and a notification added.'});
      // Leave the receipt open for the user to read.
      await page.waitForEvent('close',{timeout:0}).catch(()=>{});await browser.close();return;
    }
    await delay(1500);
  }
  emit({status:closed?'closed':'unknown',message:closed?'The employer window was closed without a confirmed receipt.':'No submission receipt was detected. Check your status on the employer site.'});
  if(!closed)await page.waitForEvent('close',{timeout:0}).catch(()=>{});
  await browser.close();
}
module.exports={fillFields,receipt,trustedPage};
if(require.main===module)main().catch(()=>{emit({status:'error',message:'Unable to start browser autofill. Use the official application link.'});process.exitCode=1;});
