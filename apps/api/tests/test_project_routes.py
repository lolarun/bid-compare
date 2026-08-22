"""Integration tests for /api/projects — the (name, code) uniqueness path.

真实缺陷（2026-08-21 服务端日志）：两个工作台（project 101/102）各自识别同一
份招标文件，WorkspaceView 把识别出的项目名回填后各写一次 PUT，第二次撞
uq_project_name_code。update_project 当时没有捕获 IntegrityError，裸的
sqlalchemy.exc.IntegrityError 以 500 + 堆栈返回。这里锁住"重名返回 409 且带
可读文案"，create 和 update 两条路径都覆盖。
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(temp_db, auth_override):
    from apps.api.main import app

    with TestClient(app) as c:
        yield c


def _create(client, name: str, code: str = ""):
    return client.post("/api/projects", json={"name": name, "code": code})


class TestProjectNameUniqueness:
    def test_create_duplicate_returns_409_with_readable_detail(self, client):
        assert _create(client, "金桥 J9A-03 阀门", "J9A-03").status_code == 201

        resp = _create(client, "金桥 J9A-03 阀门", "J9A-03")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "金桥 J9A-03 阀门" in detail
        assert "J9A-03" in detail

    def test_update_colliding_name_returns_409_not_500(self, client):
        """回归：两个工作台回填同一个识别出的项目名。"""
        first = _create(client, "识别出的项目名", "").json()
        second = _create(client, "新比价项目-1755000000000", "").json()
        assert first["id"] != second["id"]

        resp = client.put(f"/api/projects/{second['id']}", json={"name": "识别出的项目名", "code": ""})
        assert resp.status_code == 409, f"expected 409, got {resp.status_code}"
        assert "识别出的项目名" in resp.json()["detail"]

    def test_update_after_409_leaves_row_unchanged_and_session_usable(self, client):
        """rollback 之后：库里还是旧名字，且同一个 app/session 能继续服务。"""
        _create(client, "占用中的名字", "C1")
        target = _create(client, "我的项目", "C1").json()

        assert client.put(f"/api/projects/{target['id']}", json={"name": "占用中的名字"}).status_code == 409

        # 旧值没被写脏
        assert client.get(f"/api/projects/{target['id']}").json()["name"] == "我的项目"
        # 换个不冲突的名字仍然能存上（session 没有卡在失败事务里）
        ok = client.put(f"/api/projects/{target['id']}", json={"name": "我的项目（改）"})
        assert ok.status_code == 200
        assert ok.json()["name"] == "我的项目（改）"

    def test_update_same_name_on_same_row_is_not_a_collision(self, client):
        """自己改自己（回填值跟当前值相同）不能被误判成重名。"""
        proj = _create(client, "自身重复写入", "X1").json()
        resp = client.put(f"/api/projects/{proj['id']}", json={"name": "自身重复写入", "code": "X1"})
        assert resp.status_code == 200


# ── /find-exact：重名时要能找回那个"已有项目"（2026-08-21）──────────────

class TestFindExact:
    def test_returns_the_project_when_name_and_code_match_exactly(self, client):
        r = client.post("/api/projects", json={"name": "金桥地铁上盖 J9A-03", "code": ""})
        assert r.status_code in (200, 201), r.text
        pid = r.json()["id"]

        r = client.get("/api/projects/find-exact",
                       params={"name": "金桥地铁上盖 J9A-03", "code": ""})
        assert r.status_code == 200
        assert r.json()["id"] == pid

    def test_returns_null_when_nothing_matches(self, client):
        r = client.get("/api/projects/find-exact", params={"name": "查无此项目", "code": ""})
        assert r.status_code == 200
        assert r.json() is None

    def test_is_exact_not_fuzzy(self, client):
        """跟唯一约束同一个判据。模糊匹配会把「金桥17B-06」当成
        「金桥地铁上盖…」的已有项目，那是错的答案，比没有答案更糟。"""
        client.post("/api/projects", json={"name": "金桥17B-06", "code": ""})
        r = client.get("/api/projects/find-exact", params={"name": "金桥", "code": ""})
        assert r.json() is None

    def test_code_participates_in_the_match(self, client):
        """(name, code) 是复合唯一键——同名不同编号是两个项目，不能混为一谈。"""
        client.post("/api/projects", json={"name": "同名项目", "code": "A-1"})
        assert client.get("/api/projects/find-exact",
                          params={"name": "同名项目", "code": "B-2"}).json() is None
        assert client.get("/api/projects/find-exact",
                          params={"name": "同名项目", "code": "A-1"}).json() is not None
