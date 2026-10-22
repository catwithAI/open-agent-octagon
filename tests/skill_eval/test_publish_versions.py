import sqlite3
import pytest
from .test_drafts import service
from .test_imports import imported


def test_publish_new_number_keeps_beta_editable_and_freezes_full_changes(service):
    item=imported(service)
    pub=service.publish_draft(item['draft']['id'],1,1,'1.0',[{'op':'write','path':'script.sh','text':'echo new','mode':0o755}])
    assert pub['release']['status']=='published'
    assert service.get_release(item['release']['id'])['status']=='unpublished'
    assert service.get_draft(item['draft']['id'])['revision']==1
    assert pub['snapshot']['id']!=item['snapshot']['id']
    assert b'script.sh' in service.export_snapshot(pub['snapshot']['id'])
    with service.repo.transaction() as c:
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            c.execute("UPDATE skill_eval_releases SET revision=revision+1 WHERE id=?",(pub['release']['id'],))
    service.save_draft(item['draft']['id'],1,1)
    for version in ('1.0','0.0.1',''):
        with pytest.raises(Exception):service.publish_draft(item['draft']['id'],2,2,version)
