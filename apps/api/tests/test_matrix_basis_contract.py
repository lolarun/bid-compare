"""design/31 §3 cut1：矩阵结果必须自报口径，且预览口径不能给正式结论。

这些断言写在契约层是有意的——服务层"记得设成 conditional"是靠自觉，
漏的那一次不会报错，界面上就是一个看起来很正式的推荐。
"""
import pytest
from pydantic import ValidationError

from apps.api.schemas.analysis import BidMatrixResult


def _mk(**kw):
    base = dict(project_id=1, suppliers=[], rows=[], totals=[])
    base.update(kw)
    return BidMatrixResult(**base)


def test_official_is_the_default():
    """所有既有调用方一个字都没改，必须仍然是 official。"""
    m = _mk()
    assert m.basis == "official"
    assert m.preview_unconfirmed_rows == 0


def test_official_may_recommend_firmly():
    assert _mk(recommendation_level="firm").recommendation_level == "firm"


def test_preview_rejects_firm_recommendation():
    with pytest.raises(ValidationError, match="firm"):
        _mk(basis="preview", recommendation_level="firm")


def test_preview_rejects_firm_comprehensive_status():
    with pytest.raises(ValidationError, match="firm"):
        _mk(basis="preview", comprehensive_recommendation_status="firm")


@pytest.mark.parametrize("level", ["conditional", "blocked", None])
def test_preview_allows_non_firm_levels(level):
    m = _mk(basis="preview", recommendation_level=level, preview_unconfirmed_rows=3)
    assert m.basis == "preview" and m.preview_unconfirmed_rows == 3


def test_official_cannot_carry_unconfirmed_count():
    """官方结果里冒出"含 N 行未确认"是自相矛盾的，说明上游串了口径。"""
    with pytest.raises(ValidationError, match="preview_unconfirmed_rows"):
        _mk(preview_unconfirmed_rows=1)
