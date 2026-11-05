"""
Tests for restocking API endpoints (candidates, order submission, submitted orders).
"""
from datetime import datetime

import pytest

import main


@pytest.fixture(autouse=True)
def reset_submitted_orders():
    """Clear the in-memory submitted orders list so tests don't leak state into each other."""
    # Clear in place: the endpoints hold a reference to this same list object
    main.submitted_orders.clear()
    yield
    main.submitted_orders.clear()


def get_candidates(client):
    return client.get("/api/restocking/candidates").json()


def place_order(client, items, budget=1_000_000):
    return client.post("/api/restocking/orders", json={"budget": budget, "items": items})


class TestRestockingCandidates:
    """Test suite for the restocking candidates endpoint."""

    def test_get_candidates(self, client):
        """Test getting restocking candidates."""
        response = client.get("/api/restocking/candidates")
        assert response.status_code == 200

        data = response.json()
        assert "items" in data
        assert "max_budget" in data
        assert len(data["items"]) > 0

        item = data["items"][0]
        for field in ["sku", "name", "category", "trend", "quantity_on_hand",
                      "forecasted_demand", "shortfall", "unit_cost", "lead_time_days"]:
            assert field in item

    def test_candidate_shortfall_calculation(self, client):
        """Test that shortfall is forecasted demand minus stock on hand, and always positive."""
        for item in get_candidates(client)["items"]:
            assert item["shortfall"] == item["forecasted_demand"] - item["quantity_on_hand"]
            assert item["shortfall"] > 0

    def test_candidates_exclude_items_with_no_shortfall(self, client):
        """Test that items with enough stock are not recommended."""
        skus = [item["sku"] for item in get_candidates(client)["items"]]
        # PSU-501 has more stock than forecast demand; MTR-304 demand is decreasing below stock
        assert "PSU-501" not in skus
        assert "MTR-304" not in skus

    def test_max_budget_calculation(self, client):
        """Test that max_budget is the cost of covering every shortfall."""
        data = get_candidates(client)
        expected = sum(item["shortfall"] * item["unit_cost"] for item in data["items"])
        assert abs(data["max_budget"] - expected) < 0.01

    def test_candidate_lead_time_by_category(self, client):
        """Test that lead time comes from the category lookup."""
        for item in get_candidates(client)["items"]:
            expected = main.LEAD_TIME_DAYS.get(item["category"], main.DEFAULT_LEAD_TIME_DAYS)
            assert item["lead_time_days"] == expected


class TestRestockingOrders:
    """Test suite for submitting restocking orders."""

    def test_place_order(self, client):
        """Test placing a valid order."""
        item = get_candidates(client)["items"][0]
        response = place_order(client, [{"sku": item["sku"], "quantity": 2}])
        assert response.status_code == 201

        order = response.json()
        assert order["id"] == "RS-0001"
        assert order["status"] == "Submitted"
        assert len(order["items"]) == 1
        assert order["items"][0]["quantity"] == 2
        assert abs(order["total_value"] - 2 * item["unit_cost"]) < 0.01

    def test_order_uses_server_side_prices(self, client):
        """Test that line totals come from server unit costs, not the client."""
        item = get_candidates(client)["items"][0]
        order = place_order(client, [{"sku": item["sku"], "quantity": 3}]).json()
        assert order["items"][0]["unit_cost"] == item["unit_cost"]
        assert abs(order["items"][0]["line_total"] - 3 * item["unit_cost"]) < 0.01

    def test_order_lead_time_is_longest_item(self, client):
        """Test that the order lead time is the longest among its items."""
        items = get_candidates(client)["items"]
        # Pick the items with the shortest and longest lead times
        fastest = min(items, key=lambda i: i["lead_time_days"])
        slowest = max(items, key=lambda i: i["lead_time_days"])
        assert fastest["lead_time_days"] < slowest["lead_time_days"]

        order = place_order(client, [
            {"sku": fastest["sku"], "quantity": 1},
            {"sku": slowest["sku"], "quantity": 1},
        ]).json()
        assert order["lead_time_days"] == slowest["lead_time_days"]

    def test_expected_delivery_matches_lead_time(self, client):
        """Test that expected delivery is order date plus lead time."""
        item = get_candidates(client)["items"][0]
        order = place_order(client, [{"sku": item["sku"], "quantity": 1}]).json()

        placed = datetime.fromisoformat(order["order_date"])
        expected = datetime.fromisoformat(order["expected_delivery"])
        assert (expected - placed).days == order["lead_time_days"]

    def test_order_over_budget_rejected(self, client):
        """Test that an order exceeding the budget is rejected."""
        item = get_candidates(client)["items"][0]
        response = place_order(client, [{"sku": item["sku"], "quantity": 1}], budget=0.01)
        assert response.status_code == 400
        assert "budget" in response.json()["detail"].lower()

    def test_empty_order_rejected(self, client):
        """Test that an order with no items is rejected."""
        response = place_order(client, [])
        assert response.status_code == 400

    def test_unknown_sku_rejected(self, client):
        """Test that an unknown SKU is rejected."""
        response = place_order(client, [{"sku": "NOPE-999", "quantity": 1}])
        assert response.status_code == 400

    def test_sku_without_shortfall_rejected(self, client):
        """Test that a SKU with enough stock cannot be ordered."""
        response = place_order(client, [{"sku": "PSU-501", "quantity": 1}])
        assert response.status_code == 400

    @pytest.mark.parametrize("quantity", [0, -5])
    def test_non_positive_quantity_rejected(self, client, quantity):
        """Test that zero or negative quantities are rejected."""
        item = get_candidates(client)["items"][0]
        response = place_order(client, [{"sku": item["sku"], "quantity": quantity}])
        assert response.status_code == 400

    def test_quantity_above_shortfall_rejected(self, client):
        """Test that ordering more than the shortfall is rejected."""
        item = get_candidates(client)["items"][0]
        response = place_order(client, [{"sku": item["sku"], "quantity": item["shortfall"] + 1}])
        assert response.status_code == 400

    def test_duplicate_sku_rejected(self, client):
        """Test that duplicate SKUs are rejected so they can't bypass the shortfall limit."""
        item = get_candidates(client)["items"][0]
        response = place_order(client, [
            {"sku": item["sku"], "quantity": 1},
            {"sku": item["sku"], "quantity": 1},
        ])
        assert response.status_code == 400

    def test_invalid_body_rejected(self, client):
        """Test that a malformed body fails Pydantic validation."""
        response = client.post("/api/restocking/orders", json={"items": []})
        assert response.status_code == 422

    def test_rejected_order_is_not_stored(self, client):
        """Test that failed submissions don't appear in submitted orders."""
        place_order(client, [{"sku": "NOPE-999", "quantity": 1}])
        assert client.get("/api/submitted-orders").json() == []


class TestSubmittedOrders:
    """Test suite for the submitted orders endpoint."""

    def test_no_submitted_orders_initially(self, client):
        """Test that the list starts empty."""
        response = client.get("/api/submitted-orders")
        assert response.status_code == 200
        assert response.json() == []

    def test_submitted_orders_newest_first(self, client):
        """Test that submitted orders are returned newest first."""
        sku = get_candidates(client)["items"][0]["sku"]
        place_order(client, [{"sku": sku, "quantity": 1}])
        place_order(client, [{"sku": sku, "quantity": 2}])

        orders = client.get("/api/submitted-orders").json()
        assert [o["id"] for o in orders] == ["RS-0002", "RS-0001"]

    def test_submitted_order_structure(self, client):
        """Test the structure of a submitted order."""
        sku = get_candidates(client)["items"][0]["sku"]
        place_order(client, [{"sku": sku, "quantity": 1}])

        order = client.get("/api/submitted-orders").json()[0]
        for field in ["id", "items", "total_value", "budget", "lead_time_days",
                      "order_date", "expected_delivery", "status"]:
            assert field in order

    def test_existing_order_lookup_unaffected(self, client):
        """Test that GET /api/orders/{id} still works alongside the submitted-orders route."""
        order_id = client.get("/api/orders").json()[0]["id"]
        assert client.get(f"/api/orders/{order_id}").status_code == 200
