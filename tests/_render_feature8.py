from pathlib import Path
from unittest.mock import patch
from tests.test_identity_email_change import configured, ready, actor, call, NEW
from services import identity_email_change as change


def test_render(configured,admin_client):
    output=Path('/tmp/feature8-browser');output.mkdir(exist_ok=True)
    def save(name,response):
        assert response.status_code==200,response.status_code
        (output/(name+'.html')).write_bytes(response.data)
    save('queue',call(configured,admin_client,'/admin/identity/email-changes'))
    cid,tokens=ready()
    path=f'/admin/identity/email-changes/{cid}'
    save('detail',call(configured,admin_client,path))
    save('preview',call(configured,admin_client,path,method='post',data={'action':'preview'}))
    save('verify',call(configured,configured.test_client(),f"/identity/email-change/verify/{cid}/new/{tokens['new']}"))
    _,token=change.preview(actor(),cid)
    change.execute(actor(),cid,token,NEW)
    save('completed',call(configured,admin_client,path))
