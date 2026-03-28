import json

from ouroboros.tools.fatsecret import (
    _coerce_food_items,
    _coerce_servings,
    _fatsecret_food,
    _fatsecret_search,
    _get_access_token,
    _token_cache,
    get_tools,
)


class FakeCtx:
    task_id = "task-fatsecret"
    current_task_type = "task"
    event_queue = None
    pending_events = []


class DummyResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False



def test_get_tools_registers_two():
    tools = get_tools()
    assert [tool.name for tool in tools] == ["fatsecret_search", "fatsecret_food"]



def test_coerce_food_items_handles_single_dict():
    items = _coerce_food_items({
        "food": {
            "food_id": "1",
            "food_name": "Buckwheat",
            "food_description": "Per 100g - Calories: 92kcal | Fat: 0.6g | Carbs: 19g | Protein: 3.4g",
        }
    })
    assert len(items) == 1
    assert items[0]["food_id"] == "1"
    assert items[0]["food_name"] == "Buckwheat"



def test_coerce_servings_handles_single_dict():
    servings = _coerce_servings({
        "serving": {
            "serving_id": "42",
            "serving_description": "100 g",
            "calories": "92",
            "protein": "3.4",
        }
    })
    assert servings == [{
        "serving_id": "42",
        "serving_description": "100 g",
        "calories": "92",
        "protein": "3.4",
    }]



def test_get_access_token_caches(monkeypatch):
    _token_cache["access_token"] = ""
    _token_cache["expires_at"] = 0
    calls = {"count": 0}

    def fake_urlopen(req, timeout=20):
        calls["count"] += 1
        return DummyResponse({"access_token": "token-123", "expires_in": 3600})

    monkeypatch.setenv("FATSECRET_CLIENT_ID", "id")
    monkeypatch.setenv("FATSECRET_CLIENT_SECRET", "secret")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    first = _get_access_token()
    second = _get_access_token()

    assert first == "token-123"
    assert second == "token-123"
    assert calls["count"] == 1



def test_fatsecret_search_uses_translated_query(monkeypatch):
    ctx = FakeCtx()
    captured = {"search_expression": None}

    def fake_api_call(method, **params):
        assert method == "foods.search"
        captured["search_expression"] = params["search_expression"]
        return {
            "foods": {
                "food": [{
                    "food_id": "123",
                    "food_name": "Buckwheat",
                    "food_description": "Per 100g - Calories: 92kcal",
                }]
            }
        }

    monkeypatch.setattr("ouroboros.tools.fatsecret._translate_query", lambda ctx, q: ("buckwheat cooked", "mock-model"))
    monkeypatch.setattr("ouroboros.tools.fatsecret._api_call", fake_api_call)

    data = json.loads(_fatsecret_search(ctx, "гречка варёная", 5))
    assert data["query"] == "гречка варёная"
    assert data["normalized_query"] == "buckwheat cooked"
    assert data["translation_model"] == "mock-model"
    assert data["count"] == 1
    assert captured["search_expression"] == "buckwheat cooked"



def test_fatsecret_food_normalizes_servings(monkeypatch):
    ctx = FakeCtx()

    def fake_api_call(method, **params):
        assert method == "food.get.v4"
        assert params["food_id"] == "123"
        return {
            "food": {
                "food_name": "Buckwheat",
                "food_type": "Generic",
                "servings": {
                    "serving": {
                        "serving_id": "1",
                        "serving_description": "100 g",
                        "calories": "92",
                        "carbohydrate": "19",
                        "protein": "3.4",
                        "fat": "0.6",
                    }
                },
            }
        }

    monkeypatch.setattr("ouroboros.tools.fatsecret._api_call", fake_api_call)

    data = json.loads(_fatsecret_food(ctx, "123"))
    assert data["food_id"] == "123"
    assert data["food_name"] == "Buckwheat"
    assert data["servings"][0]["serving_description"] == "100 g"
    assert data["servings"][0]["calories"] == "92"



def test_fatsecret_search_requires_query():
    data = json.loads(_fatsecret_search(FakeCtx(), "", 5))
    assert "error" in data
