"""Service-restart recovery without relaxing CSRF or replaying uncertain writes."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from portfolio_test_fixture import account_fixture
from web_dashboard import create_app,CSRF
from test_strategy_ui import StrategyDashboardTests

class SessionSecurityTests(unittest.TestCase):
    def test_token_rotation_and_cross_site_rejection(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'config.json').write_text(json.dumps(account_fixture()))
            client=create_app(root).test_client();headers={'Host':'localhost'}
            token=client.get('/api/session',headers=headers)
            self.assertEqual(token.status_code,200);self.assertEqual(token.json['csrf_token'],CSRF)
            self.assertEqual(token.headers['Cache-Control'],'no-store')
            self.assertNotIn('Access-Control-Allow-Origin',token.headers)
            for extra in [{'Origin':'https://example.com'},{'Sec-Fetch-Site':'cross-site'},{'Host':'example.com'}]:
                self.assertEqual(client.get('/api/session',headers={**headers,**extra}).status_code,403)
            with patch('strategy_runtime.prepare_bounded',return_value={'ok':False,'blockers':['test']}) as prepare:
                response=client.post('/api/strategy/prepare/002463',headers={**headers,'X-CSRF-Token':'before-restart'},json={})
                self.assertEqual(response.status_code,403);self.assertEqual(response.json['code'],'csrf_expired');prepare.assert_not_called()
                response=client.post('/api/strategy/prepare/002463',headers={**headers,'X-CSRF-Token':CSRF},json={})
                self.assertEqual(response.status_code,200);prepare.assert_called_once()
                response=client.post('/api/strategy/prepare/002463',headers={**headers,'X-CSRF-Token':CSRF,'Origin':'https://example.com'},json={})
                self.assertEqual(response.status_code,403);prepare.assert_called_once()

class BrowserRecoveryTests(unittest.TestCase):
    run_js=StrategyDashboardTests.run_js
    def test_expired_request_is_retried_once_with_same_payload(self):
        self.run_js(r'''
(async()=>{
const calls=[];fetch=async(path,o)=>{calls.push([path,o]);if(path==='/api/session')return {ok:true,json:async()=>({csrf_token:'new-token'})};if(o.headers['X-CSRF-Token']==='csrf')return {ok:false,status:403,json:async()=>({code:'csrf_expired'})};return {ok:true,status:200,json:async()=>({ok:true})};};
const result=await api('/api/strategy/prepare/002463','POST',{a:1});
if(!result.ok||calls.length!==3)throw Error('did not recover once');
if(calls[0][1].body!==calls[2][1].body||calls[2][1].headers['X-CSRF-Token']!=='new-token')throw Error('payload/token mismatch');
})()
''')
    def test_concurrent_failures_share_refresh(self):
        self.run_js(r'''
(async()=>{
let renewals=0,actions=0;
fetch=async(path,o)=>{if(path==='/api/session'){renewals++;await Promise.resolve();return {ok:true,json:async()=>({csrf_token:'new-token'})};}actions++;return o.headers['X-CSRF-Token']==='csrf'?{ok:false,status:403,json:async()=>({code:'csrf_expired'})}:{ok:true,json:async()=>({ok:true})};};
await Promise.all([api('/a','PUT',{}),api('/b','POST',{})]);if(renewals!==1||actions!==4)throw Error('duplicate token recovery');
})()
''')
    def test_other_errors_are_never_replayed(self):
        self.run_js(r'''
(async()=>{
for(const kind of ['network','forbidden','server']){
let calls=0;fetch=async()=>{calls++;if(kind==='network')throw Error('lost response');return {ok:false,status:kind==='forbidden'?403:500,json:async()=>({error:'failed'})};};
try{await api('/write','POST',{});throw Error('unexpected success');}catch(e){if(calls!==1)throw Error('unsafe replay '+kind);}
}
})()
''')
    def test_repeated_expiry_is_bounded(self):
        self.run_js(r'''
(async()=>{
let renewals=0,actions=0;fetch=async(path)=>{if(path==='/api/session'){renewals++;return {ok:true,json:async()=>({csrf_token:'new'})};}actions++;return {ok:false,status:403,json:async()=>({code:'csrf_expired',error:'expired'})};};
let failed=false;try{await api('/write','POST',{});}catch(e){failed=true;}if(!failed||renewals!==1||actions!==2)throw Error('unbounded expiry retry');
})()
''')

if __name__=='__main__':unittest.main()
