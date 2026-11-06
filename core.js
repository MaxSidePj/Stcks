export const GOAL_KM = 16.09344;
export const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
export function isoDate(value = new Date()) {
  return `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,'0')}-${String(value.getDate()).padStart(2,'0')}`;
}
export function dateOf(s) { const [y,m,d] = s.split('-').map(Number); return new Date(y,m-1,d,12); }
export function shift(s,n) { const d=dateOf(s); d.setDate(d.getDate()+n); return isoDate(d); }
export function daysBetween(a,b) { return Math.round((dateOf(b)-dateOf(a))/86400000); }
export function weekStart(s) { const d=dateOf(s); return shift(s,-((d.getDay()+6)%7)); }
export function validDate(s) { return typeof s==='string' && /^\d{4}-\d{2}-\d{2}$/.test(s) && isoDate(dateOf(s))===s; }
export function validRun(input, today=isoDate()) {
  const r={...input, distanceKm:Number(input.distanceKm), seconds:Number(input.seconds)};
  if(r.id==null||!String(r.id).trim())throw new Error('This activity has no identifier.');
  if(!validDate(r.date)||r.date>today) throw new Error('Choose a valid run date, today or earlier.');
  if(!Number.isFinite(r.distanceKm)||r.distanceKm<=0||r.distanceKm>300) throw new Error('Distance must be between 0 and 300 km.');
  if(!Number.isFinite(r.seconds)||r.seconds<=0||r.seconds>172800) throw new Error('Enter a valid duration.');
  if(r.effort!=null && (!Number.isFinite(Number(r.effort))||Number(r.effort)<1||Number(r.effort)>10)) throw new Error('Effort must be between 1 and 10.');
  return {id:String(r.id), date:r.date, distanceKm:r.distanceKm, seconds:r.seconds,
    sport:r.sport==='walk'?'walk':'run',
    title:String(r.title||(r.sport==='walk'?'Walk':'Run')).slice(0,120), effort:r.effort==null?null:Number(r.effort),
    pain:r.pain===true, notes:String(r.notes||'').slice(0,1000), source:r.source||'manual',
    importKey:r.importKey?String(r.importKey):null, timing:r.timing||'moving'};
}
export function validProfile(p,today=isoDate()) {
  const weeklyKm=Number(p.weeklyKm), longestKm=Number(p.longestKm), currentDays=Number(p.currentDays);
  const days=[...new Set((p.days||[]).map(Number))].filter(d=>Number.isInteger(d)&&d>=0&&d<=6).sort();
  const targetDays=Number(p.targetDays||Math.min(3,days.length));
  if(!Number.isFinite(weeklyKm)||weeklyKm<0||weeklyKm>250) throw new Error('Enter your usual weekly distance (0 if starting).');
  if(!Number.isFinite(longestKm)||longestKm<0||longestKm>100||longestKm>weeklyKm) throw new Error('Your longest run cannot exceed your usual weekly total.');
  if(weeklyKm>0&&longestKm===0)throw new Error('Enter the distance of a recent longest run.');
  if(!Number.isInteger(currentDays)||currentDays<0||currentDays>6) throw new Error('Choose how often you currently run.');
  if((weeklyKm===0)!==(currentDays===0)) throw new Error('Use 0 km and 0 current runs if you are starting; otherwise enter both.');
  if(days.length<2||days.length>5) throw new Error('Choose between two and five available training days.');
  if(!Number.isInteger(targetDays)||targetDays<2||targetDays>5||targetDays>days.length)throw new Error('Choose 2–5 runs per week and at least that many available days.');
  if(p.raceDate && (!validDate(p.raceDate)||p.raceDate<=today)) throw new Error('Choose a future race date, or leave it blank.');
  return {weeklyKm,longestKm,currentDays,targetDays,days,raceDate:p.raceDate||null,created:p.created||today};
}
export function summary(runs,today=isoDate()) {
  runs=runs.filter(r=>r.sport!=='walk');
  const recent=runs.filter(r=>r.date<=today&&r.date>=shift(today,-27));
  const week=runs.filter(r=>r.date>=weekStart(today)&&r.date<=today);
  return {weekKm:week.reduce((s,r)=>s+r.distanceKm,0),weekRuns:week.length,
    longest:Math.max(0,...recent.map(r=>r.distanceKm)),totalKm:runs.reduce((s,r)=>s+r.distanceKm,0),
    totalRuns:runs.length,recentKm:recent.reduce((s,r)=>s+r.distanceKm,0),
    weeks:Array.from({length:8},(_,i)=>{const start=shift(weekStart(today),-7*(7-i));return {start,km:runs.filter(r=>r.date>=start&&r.date<shift(start,7)).reduce((s,r)=>s+r.distanceKm,0)};})};
}
export function storedProfile(p,today=isoDate()) {
  if(!p)return null;
  if(p.raceDate&&!validDate(p.raceDate))throw new Error('The saved race date is invalid.');
  return {...validProfile({...p,raceDate:null,created:validDate(p.created)&&p.created<=today?p.created:today},today),raceDate:p.raceDate||null};
}
export function suggest(runs,profile,checkin={},today=isoDate()) {
  if(!profile) return {kind:'setup',title:'Make this plan yours',body:'Set your current running level and available days to get your next-session suggestion.',sessions:[]};
  const daysToRace=profile.raceDate?daysBetween(today,profile.raceDate):null;
  if(daysToRace!=null&&daysToRace<0) return {kind:'setup',title:'Choose your next goal',body:'Your race date has passed. Update your goal before using new training suggestions.',sessions:[]};
  const allActivities=runs;
  runs=runs.filter(r=>r.sport!=='walk');
  const recent=[...runs].filter(r=>r.date<=today).sort((a,b)=>b.date.localeCompare(a.date));
  const last=recent[0], feeling=checkin.date===today?checkin.feeling:'normal';
  if(feeling==='pain'||allActivities.some(r=>r.pain&&daysBetween(r.date,today)>=0&&daysBetween(r.date,today)<=2)) return {kind:'rest',title:'Pause the running plan',body:'You reported pain. Rest from running and reassess; get advice from a clinician or physiotherapist if pain persists. This app cannot assess an injury.',sessions:[]};
  const stat=summary(runs,today), start=weekStart(today);
  const available=profile.days;
  const frequency=Math.min(available.length,profile.currentDays===0?Math.min(3,profile.targetDays):profile.targetDays);
  const selected=available.length<=frequency?available:[...available].filter((_,i)=>i===0||i===available.length-1||i%2===0).slice(0,frequency);
  for(const d of available) if(selected.length<frequency&&!selected.includes(d))selected.push(d);
  const complete=[];
  for(let w=1;w<=2;w++){const a=shift(start,-7*w),b=shift(a,7);complete.push(runs.filter(r=>r.date>=a&&r.date<b));}
  let target=profile.weeklyKm*Math.min(1,frequency/Math.max(1,profile.currentDays));
  let increase=false;
  let reason='Use your recent baseline, scaled down if you choose fewer runs. Missed kilometres are not added to later sessions.';
  if(complete.every(w=>w.length>=frequency&&w.reduce((s,r)=>s+r.distanceKm,0)>=profile.weeklyKm*.9&&w.every(r=>!r.pain&&r.effort!=null&&r.effort<=6))&&feeling!=='tired') {
    const average=complete.reduce((s,w)=>s+w.reduce((n,r)=>n+r.distanceKm,0),0)/2;
    target=Math.min(average*1.05,average+1);
    increase=true;
    reason='Two logged weeks were consistent and felt manageable. A small volume increase is optional, not a target you must hit.';
  }
  const baseTarget=target;
  const weeks=Math.floor(daysBetween(profile.created,today)/7);
  if(weeks>0&&weeks%4===3){target*=.85;increase=false;reason='A lighter week to consolidate training.';}
  if(feeling==='tired'){target=Math.min(target,profile.weeklyKm)*.8;increase=false;reason='You feel tired today. Reduce this week and keep the next session easy.';}
  if(daysToRace!=null&&daysToRace<=7){target=profile.weeklyKm*.5;increase=false;reason='Race week: reduce training, keep sessions easy, and do not make up missed distance.';}
  target=Math.round(target*10)/10;
  const beginner=profile.weeklyKm===0;
  const longBase=Math.max(profile.longestKm,stat.longest);
  const longKm=Math.min(GOAL_KM,Math.max(0,Math.min(longBase?longBase+(increase ? 0.5 : 0):target*.35,target*.45)));
  const ordinary=frequency>1?Math.min(longBase||target,Math.max(0,(target-longKm)/(frequency-1))):target;
  const sessions=[];let earliest=today;
  const recovery=last?.effort>=7||feeling==='tired'?3:frequency<=3?2:1;
  if(last&&daysBetween(last.date,today)<recovery) earliest=shift(last.date,recovery);
  let remaining=Math.max(0,target-stat.weekKm),remainingRuns=Math.max(0,frequency-stat.weekRuns);
  const nextWeek=shift(start,7);
  const stage0=beginner?Math.floor(runs.filter(r=>r.date>=profile.created&&r.date<start).length/Math.max(2,frequency)):null;
  for(let offset=0;shift(today,offset)<nextWeek&&sessions.length<frequency;offset++){
    const d=shift(today,offset);
    if(d<earliest||!selected.includes(dateOf(d).getDay())||runs.some(r=>r.date===d))continue;
    if(profile.raceDate&&d>=profile.raceDate)continue;
    const thisWeek=d<shift(start,7);
    if(thisWeek&&remainingRuns===0)continue;
    if(sessions.length&&daysBetween(sessions.at(-1).date,d)<(frequency<=3||sessions.at(-1).type==='Easy long run'?2:1))continue;
    const isLong=!beginner&&selected.length>1&&dateOf(d).getDay()===[...selected].sort((a,b)=>((a+6)%7)-((b+6)%7)).at(-1);
    const proposed=isLong?longKm:ordinary;
    const km=beginner?null:Math.round(Math.min(proposed,thisWeek?remaining:target)*10)/10;
    if(!beginner&&km<1)continue;
    sessions.push(beginner?{date:d,...runWalk(stage0)}:isLong?{date:d,km,long:true,...longRun(km)}:{date:d,type:'Easy run',km,detail:EASY});
    if(thisWeek){remaining=Math.max(0,remaining-(km||0));remainingRuns--;}
  }
  const raceWeek=profile.raceDate?weekStart(profile.raceDate):null;
  const phase0=raceWeek===start?'race':raceWeek===nextWeek?'taper':weeks%4===3?'lighter':beginner?'run-walk':'build';
  const q0=beginner||feeling==='tired'||last?.effort>=7?null:quality(longBase,weeks,phase0,frequency);
  const slot=sessions.find(s=>!s.long);
  if(q0&&slot)Object.assign(slot,q0,{quality:true});
  if(raceWeek===start)sessions.push(raceDay(profile.raceDate));
  const baseLong=Math.min(GOAL_KM,Math.max(0,Math.min(longBase?longBase+(increase?.5:0):baseTarget*.35,baseTarget*.45)));
  const weekPlan=[{start,phase:phase0,km:beginner?null:target,sessions},...projectWeeks({profile,start,frequency,
    ordered:[...selected].sort((a,b)=>((a+6)%7)-((b+6)%7)),baseKm:baseTarget,baseLong,absStart:weeks,stage:stage0,
    lastDate:sessions.filter(s=>!s.race).at(-1)?.date||last?.date,earliest})];
  const next=weekPlan.flatMap(w=>w.sessions)[0];
  let body=reason;
  if(beginner&&stage0>=STAGES.length)body+=' You have completed the run/walk build-up. Update Settings with your usual weekly kilometres to get distance targets.';
  if(beginner&&profile.targetDays>3)body+=' While starting from zero, use up to three run/walk sessions and use extra days for gentle walking or recovery.';
  if(available.length>selected.length||profile.currentDays>available.length)body+=' Frequency is limited by your available days and current habit.';
  if(daysToRace!=null&&daysToRace<56&&(beginner||longBase<8))body+=' The race is close relative to your current long run. Do not accelerate this plan to meet the date; consider a run/walk goal or a later event.';
  if(next&&next.date>today)body+=' Today can be a rest day.';
  return {kind:beginner?'beginner':'train',title:next?`${next.type}${next.km!=null?' · '+next.km.toFixed(1)+' km':next.minutes?' · '+next.minutes+' min':''}`:'Keep the remaining days easy',
    body,next,sessions:weekPlan.slice(0,2).flatMap(w=>w.sessions),weeks:weekPlan,outlook:outlook(weekPlan,profile),targetKm:beginner?null:target,frequency,daysToRace,
    note:'Suggestions are conservative planning rules, not a validated personalised coaching programme. They do not assess race readiness.'};
}
const EASY='Conversational effort. Finish feeling you could keep going.';
export const STAGES=[['8 × (1 min easy jog + 90 sec walk)',20],['6 × (2 min easy jog + 2 min walk)',24],['5 × (3 min easy jog + 2 min walk)',25],
  ['4 × (5 min easy jog + 2 min walk)',28],['3 × (8 min easy jog + 2 min walk)',30],['2 × (12 min easy jog + 2 min walk)',28],
  ['20 min continuous easy jog',20],['25 min continuous easy jog',25],['30 min continuous easy jog',30]];
function runWalk(stage){const [text,minutes]=STAGES[Math.min(stage,STAGES.length-1)];
  return {type:'Run / walk',km:null,minutes:minutes+10,detail:`5 min walk, then ${text}, then 5 min walk. Repeat a stage if it felt hard. There is no rush.`};}
function longRun(km){return km>=GOAL_KM-.05?{type:'10-mile run',detail:'Your goal distance at an easy, conversational pace. Walk breaks are fine.'}
  :{type:'Easy long run',detail:'Conversational pace the whole way; walk breaks are fine.'+(km>=12?' Practise drinking and fuelling as you would on race day.':'')};}
function raceDay(date){return {date,type:'Race day · 10 miles',km:GOAL_KM,race:true,detail:'Start slower than feels necessary and settle in. Walk breaks are allowed. Skip it if you have pain.'};}
export function quality(longKm,absWeek,phase,frequency){
  const strides={type:'Easy run + strides',detail:'Easy conversational run, then 6 × 20 sec quick-but-relaxed strides with a full walk back. Not a sprint.'};
  if(phase==='race')return {type:'Easy run + strides',detail:'Short and relaxed, then 4 × 15 sec smooth strides with a full walk back. Save your legs for race day.'};
  if(frequency<3||absWeek<2||phase==='lighter')return strides;
  if(phase==='taper')return {type:'Short intervals',detail:'10 min easy, 4 × 2 min at a strong 10K effort with 2 min easy jog between, 10 min easy. Crisp, not exhausting.'};
  const p=longKm/GOAL_KM;
  if(p<.4)return strides;
  if(p<.7)return {type:'Intervals',detail:`10 min easy, then ${4+Math.min(2,Math.floor((p-.4)/.1))} × 3 min hard-but-controlled (about 10K effort; only a few words possible) with 2 min easy jog between, then 10 min easy.`};
  return {type:'Tempo run',detail:`10 min easy, then ${Math.min(25,15+5*Math.floor((p-.7)/.1))} min “comfortably hard” (about 10-mile race effort; short phrases only), then 10 min easy.`};
}
const half=x=>Math.round(x*2)/2;
function projectWeeks({profile,start,frequency,ordered,baseKm,baseLong,absStart,stage,lastDate,earliest}){
  const raceWeek=profile.raceDate?weekStart(profile.raceDate):null,ratio=frequency<=3?.5:.45,result=[];
  const limit=raceWeek?Math.min(52,daysBetween(start,raceWeek)/7):30;
  let W=baseKm,L=baseLong,graduated=stage==null,hold=true,prev=lastDate;
  for(let i=1;i<=limit;i++){
    const ws=shift(start,7*i),abs=absStart+i,toRace=raceWeek?daysBetween(ws,raceWeek)/7:null;
    const walk=stage!=null&&stage+i<STAGES.length?stage+i:null;
    if(walk==null&&!graduated){graduated=true;hold=true;W=Math.max(W,frequency*3.5);L=Math.max(L,4.5);}
    const phase=toRace===0?'race':walk!=null?'run-walk':toRace===1?'taper':abs%4===3?'lighter':'build';
    if(phase==='build'&&!hold&&L<GOAL_KM-.05){W=Math.min(W*1.08,W+2);L=Math.max(L,Math.min(GOAL_KM,L+1,ratio*W));}
    if(phase==='build')hold=false;
    let w=W,l=L;
    if(phase==='lighter'){w=W*.8;l=Math.max(L*.75,L-2);}
    if(phase==='taper'){w=W*.7;l=L*.7;}
    let dates=ordered.map(d=>shift(ws,(d+6)%7)),sessions;
    if(phase==='race'){
      sessions=dates.filter(d=>d<shift(profile.raceDate,-1)).slice(0,Math.min(2,frequency-1)).map((d,n)=>walk!=null?{date:d,...runWalk(walk)}
        :{date:d,km:half(Math.max(2,Math.min(6,L*.4))),...(n===0?{...quality(L,abs,'race',frequency),quality:true}:{type:'Easy run',detail:EASY})});
      sessions.push(raceDay(profile.raceDate));
    }else if(walk!=null)sessions=dates.map(d=>({date:d,...runWalk(walk)}));
    else{
      const other=half(Math.min(l,(w-l)/Math.max(1,dates.length-1))),q=quality(L,abs,phase,frequency);
      sessions=dates.map((d,n)=>n===dates.length-1?{date:d,km:l>=GOAL_KM-.05?GOAL_KM:half(l),long:true,...longRun(l)}
        :n===0&&q?{date:d,km:other,...q,quality:true}:{date:d,km:other,type:'Easy run',detail:EASY}).filter(s=>s.km>=1);
    }
    sessions=sessions.filter(s=>{if(s.race)return true;if(s.date<earliest||(prev&&daysBetween(prev,s.date)<(frequency<=3?2:1)))return false;prev=s.date;return true;});
    result.push({start:ws,phase,km:walk!=null?null:half(sessions.reduce((n,s)=>n+(s.race?0:s.km),0)),longKm:walk!=null?null:half(l),sessions});
    if(!raceWeek&&phase==='build'&&l>=GOAL_KM-.05)break;
  }
  return result;
}
function outlook(weeks,profile){
  const longs=weeks.filter(w=>!['race','taper'].includes(w.phase)).flatMap(w=>w.sessions.filter(s=>s.long));
  const peak=Math.max(0,...longs.map(s=>s.km)),goal=longs.find(s=>s.km>=GOAL_KM-.05);
  if(profile.raceDate)return peak>=GOAL_KM*.8?`Your long run builds to about ${peak.toFixed(1)} km before tapering into race day.`
    :`At this gradual build your longest run reaches about ${peak.toFixed(1)} km before race day. Plan walk breaks for the race or choose a later event rather than speeding the plan up.`;
  return goal?`At this gradual build you would cover the full 10 miles around ${dateOf(goal.date).toLocaleDateString(undefined,{day:'numeric',month:'long',year:'numeric'})}. Add a race date in Settings to taper towards it.`
    :'Add a race date in Settings to see a plan that tapers towards it.';
}
export function parseCSV(text) {
  const rows=[];let row=[],cell='',quoted=false;
  for(let i=0;i<text.length;i++){const c=text[i];if(c==='"'){if(quoted&&text[i+1]==='"'){cell+='"';i++;}else quoted=!quoted;}
    else if(c===','&&!quoted){row.push(cell);cell='';}else if((c==='\n'||c==='\r')&&!quoted){if(c==='\r'&&text[i+1]==='\n')i++;row.push(cell);if(row.some(Boolean))rows.push(row);row=[];cell='';}else cell+=c;}
  if(quoted)throw new Error('The CSV has an unfinished quoted field.');
  row.push(cell);if(row.some(Boolean))rows.push(row);return rows;
}
function seconds(value){const s=String(value||'').trim();if(/^\d+(\.\d+)?$/.test(s))return Number(s);const a=s.split(':').map(Number);return a.every(Number.isFinite)&&a.length>=2&&a.length<=3?a.reduce((n,v)=>n*60+v,0):NaN;}
export function importCSV(text,unit='km',today=isoDate()) {
  const [headers,...data]=parseCSV(text.replace(/^\uFEFF/,''));if(!headers)throw new Error('The CSV is empty.');
  const names=headers.map(h=>h.trim().toLowerCase());
  const find=(row,keys)=>{for(const key of keys){const i=names.indexOf(key);if(i>=0&&row[i]!=='')return row[i];}return '';};
  const result=[];
  for(const row of data){const type=find(row,['activity type','sport_type','type']);if(type&&!['run','running','trailrun','virtualrun','trail run','walk','walking'].includes(type.toLowerCase()))continue;
    const rawDate=find(row,['date','activity date','start_date_local']);
    let d=rawDate.slice(0,10);if(!validDate(d)){const parsed=new Date(rawDate);if(!Number.isFinite(parsed.getTime()))throw new Error('A CSV date could not be read. Use an English Strava export or YYYY-MM-DD dates.');d=isoDate(parsed);}
    const distance=Number(find(row,['distance','distancekm','distance_km']).replace(',','.'))*(unit==='mi'?1.609344:unit==='m'?.001:1);
    const duration=seconds(find(row,['moving time','moving_time','seconds','elapsed time']));
    const external=find(row,['activity id','activity_id']);
    const key=external?'strava:'+external:`file:${d}:${distance.toFixed(3)}:${Math.round(duration)}`;
    result.push(validRun({id:key,importKey:key,date:d,distanceKm:distance,seconds:duration,sport:type.toLowerCase().startsWith('walk')?'walk':'run',title:find(row,['activity name','name','title'])||'Imported run',effort:find(row,['effort'])||null,notes:find(row,['notes']),pain:find(row,['pain']).toLowerCase()==='true',source:'file',timing:names.includes('moving time')||names.includes('moving_time')?'moving':'elapsed'},today));
  }
  return result;
}
export function mergeImported(existing,incoming){const keys=new Set(existing.map(r=>r.importKey).filter(Boolean));const added=incoming.filter(r=>{if(keys.has(r.importKey))return false;keys.add(r.importKey);return true;});return {runs:[...existing,...added],added:added.length,duplicates:incoming.length-added.length};}
