import pytest
from .test_drafts import service
from .test_imports import imported
from backend.skill_eval.service import SkillService


def test_named_experiments_are_distinct_rename_keeps_skill(service):
    skill=imported(service)['skill']['id']
    a=service.create_experiment(skill,'one');b=service.create_experiment(skill,'one')
    assert a['id']!=b['id']
    assert len(service.list_experiments(skill))==2
    renamed=service.rename_experiment(a['id'],'renamed')
    assert renamed['skill_id']==skill
    assert renamed['name']=='renamed'
    other=SkillService(service.repo.db_path,service.store.root.parent,scope='other')
    assert other.list_skills()==[]
    with pytest.raises(Exception):other.get_experiment(a['id'])
