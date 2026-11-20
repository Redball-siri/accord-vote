// 主網站伺服器端範例：不需第三方套件。請勿打包進瀏覽器。
import {createHmac,randomUUID} from 'node:crypto';
export function createAccordTicket(authenticatedEmployeeId){
  const secret=process.env.ACCORD_SSO_SECRET;
  if(!secret||secret.length<32)throw Error('缺少登入簽章密鑰');
  if(typeof authenticatedEmployeeId!=='string'||!authenticatedEmployeeId)throw Error('需提供已登入員工的不變 ID');
  const now=Math.floor(Date.now()/1000);
  const b64=obj=>Buffer.from(JSON.stringify(obj)).toString('base64url');
  const payload={iss:process.env.ACCORD_SSO_ISSUER||'company-main',aud:process.env.ACCORD_SSO_AUDIENCE||'accord',sub:authenticatedEmployeeId,iat:now,exp:now+120,jti:randomUUID()};
  const input=b64({alg:'HS256',typ:'JWT'})+'.'+b64(payload);
  return input+'.'+createHmac('sha256',secret).update(input).digest('base64url');
}
export async function syncAccordRoster({revision,employees}){
  const url=process.env.ACCORD_BASE_URL,secret=process.env.ACCORD_DIRECTORY_TOKEN;
  if(!url||!secret||secret.length<32)throw Error('缺少投票系統網址或名冊同步密鑰');
  const response=await fetch(new URL('/api/integration/roster',url),{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+secret},body:JSON.stringify({revision,employees}),redirect:'error'});
  const result=await response.json();if(!response.ok)throw Error(result.error||'同步失敗');return result;
}
