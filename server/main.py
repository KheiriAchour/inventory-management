from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from pydantic import BaseModel
from mock_data import inventory_items, orders, demand_forecasts, backlog_items, spending_summary, monthly_spending, category_spending, recent_transactions, purchase_orders

app = FastAPI(title="Factory Inventory Management System")

# Quarter mapping for date filtering
QUARTER_MAP = {
    'Q1-2025': ['2025-01', '2025-02', '2025-03'],
    'Q2-2025': ['2025-04', '2025-05', '2025-06'],
    'Q3-2025': ['2025-07', '2025-08', '2025-09'],
    'Q4-2025': ['2025-10', '2025-11', '2025-12']
}

def filter_by_month(items: list, month: Optional[str]) -> list:
    """Filter items by month/quarter based on order_date field"""
    if not month or month == 'all':
        return items

    if month.startswith('Q'):
        # Handle quarters
        if month in QUARTER_MAP:
            months = QUARTER_MAP[month]
            return [item for item in items if any(m in item.get('order_date', '') for m in months)]
    else:
        # Direct month match
        return [item for item in items if month in item.get('order_date', '')]

    return items

def apply_filters(items: list, warehouse: Optional[str] = None, category: Optional[str] = None,
                 status: Optional[str] = None) -> list:
    """Apply common filters to a list of items"""
    filtered = items

    if warehouse and warehouse != 'all':
        filtered = [item for item in filtered if item.get('warehouse') == warehouse]

    if category and category != 'all':
        filtered = [item for item in filtered if item.get('category', '').lower() == category.lower()]

    if status and status != 'all':
        filtered = [item for item in filtered if item.get('status', '').lower() == status.lower()]

    return filtered

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class InventoryItem(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    warehouse: str
    quantity_on_hand: int
    reorder_point: int
    unit_cost: float
    location: str
    last_updated: str

class Order(BaseModel):
    id: str
    order_number: str
    customer: str
    items: List[dict]
    status: str
    order_date: str
    expected_delivery: str
    total_value: float
    actual_delivery: Optional[str] = None
    warehouse: Optional[str] = None
    category: Optional[str] = None

class DemandForecast(BaseModel):
    id: str
    item_sku: str
    item_name: str
    current_demand: int
    forecasted_demand: int
    trend: str
    period: str
    # Cost, category and stock live on the forecast record because most forecast
    # SKUs have no inventory.json entry. Optional so older data still validates.
    unit_cost: Optional[float] = None
    category: Optional[str] = None
    quantity_on_hand: Optional[int] = None

class BacklogItem(BaseModel):
    id: str
    order_id: str
    item_sku: str
    item_name: str
    quantity_needed: int
    quantity_available: int
    days_delayed: int
    priority: str
    has_purchase_order: Optional[bool] = False

class PurchaseOrder(BaseModel):
    id: str
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    status: str
    created_date: str
    notes: Optional[str] = None

class CreatePurchaseOrderRequest(BaseModel):
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    notes: Optional[str] = None

# Restocking models
class RestockingCandidate(BaseModel):
    sku: str
    name: str
    category: str
    trend: str
    quantity_on_hand: int
    forecasted_demand: int
    shortfall: int
    unit_cost: float
    lead_time_days: int

class RestockingCandidates(BaseModel):
    items: List[RestockingCandidate]
    max_budget: float

class RestockingOrderItemRequest(BaseModel):
    sku: str
    quantity: int

class CreateRestockingOrderRequest(BaseModel):
    budget: float
    items: List[RestockingOrderItemRequest]

class SubmittedOrderItem(BaseModel):
    sku: str
    name: str
    category: str
    quantity: int
    unit_cost: float
    line_total: float
    lead_time_days: int

class SubmittedOrder(BaseModel):
    id: str
    items: List[SubmittedOrderItem]
    total_value: float
    budget: float
    lead_time_days: int
    order_date: str
    expected_delivery: str
    status: str

# Delivery lead time in days, by item category
LEAD_TIME_DAYS = {
    'Actuators': 14,
    'Circuit Boards': 10,
    'Controllers': 21,
    'Power Supplies': 7,
    'Sensors': 10,
}
DEFAULT_LEAD_TIME_DAYS = 14

# Restocking orders placed since startup. This is the app's only mutable state;
# it lives in memory, so it resets when the server restarts.
submitted_orders: List[dict] = []

def get_restocking_candidates_data() -> list:
    """Forecast items that need restocking: forecasted demand exceeds stock on hand."""
    candidates = []
    for forecast in demand_forecasts:
        unit_cost = forecast.get('unit_cost')
        on_hand = forecast.get('quantity_on_hand')
        # Skip records without cost/stock data; there is nothing to price them with
        if unit_cost is None or on_hand is None:
            continue
        shortfall = max(forecast['forecasted_demand'] - on_hand, 0)
        if shortfall == 0:
            continue
        category = forecast.get('category') or ''
        candidates.append({
            'sku': forecast['item_sku'],
            'name': forecast['item_name'],
            'category': category,
            'trend': forecast['trend'],
            'quantity_on_hand': on_hand,
            'forecasted_demand': forecast['forecasted_demand'],
            'shortfall': shortfall,
            'unit_cost': unit_cost,
            'lead_time_days': LEAD_TIME_DAYS.get(category, DEFAULT_LEAD_TIME_DAYS),
        })
    return candidates

# API endpoints
@app.get("/")
def root():
    return {"message": "Factory Inventory Management System API", "version": "1.0.0"}

@app.get("/api/inventory", response_model=List[InventoryItem])
def get_inventory(
    warehouse: Optional[str] = None,
    category: Optional[str] = None
):
    """Get all inventory items with optional filtering"""
    return apply_filters(inventory_items, warehouse, category)

@app.get("/api/inventory/{item_id}", response_model=InventoryItem)
def get_inventory_item(item_id: str):
    """Get a specific inventory item"""
    item = next((item for item in inventory_items if item["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item

@app.get("/api/orders", response_model=List[Order])
def get_orders(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get all orders with optional filtering"""
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)
    return filtered_orders

@app.get("/api/orders/{order_id}", response_model=Order)
def get_order(order_id: str):
    """Get a specific order"""
    order = next((order for order in orders if order["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.get("/api/demand", response_model=List[DemandForecast])
def get_demand_forecasts():
    """Get demand forecasts"""
    return demand_forecasts

@app.get("/api/backlog", response_model=List[BacklogItem])
def get_backlog():
    """Get backlog items with purchase order status"""
    # Add has_purchase_order flag to each backlog item
    result = []
    for item in backlog_items:
        item_dict = dict(item)
        # Check if this backlog item has a purchase order
        has_po = any(po["backlog_item_id"] == item["id"] for po in purchase_orders)
        item_dict["has_purchase_order"] = has_po
        result.append(item_dict)
    return result

@app.get("/api/restocking/candidates", response_model=RestockingCandidates)
def get_restocking_candidates():
    """Forecast items needing restock, plus the cost of covering every shortfall (slider maximum)"""
    items = get_restocking_candidates_data()
    max_budget = round(sum(item['shortfall'] * item['unit_cost'] for item in items), 2)
    return {"items": items, "max_budget": max_budget}

@app.post("/api/restocking/orders", response_model=SubmittedOrder, status_code=201)
def create_restocking_order(request: CreateRestockingOrderRequest):
    """Submit a restocking order. Prices and lead times come from server data, never from the client."""
    if not request.items:
        raise HTTPException(status_code=400, detail="Order must contain at least one item")

    candidates = {c['sku']: c for c in get_restocking_candidates_data()}
    seen_skus = set()
    order_items = []

    for requested in request.items:
        # Reject duplicate SKUs so a split quantity cannot bypass the shortfall check below
        if requested.sku in seen_skus:
            raise HTTPException(status_code=400, detail=f"Duplicate SKU {requested.sku}")
        seen_skus.add(requested.sku)

        candidate = candidates.get(requested.sku)
        if not candidate:
            raise HTTPException(status_code=400, detail=f"SKU {requested.sku} is not available for restocking")
        if requested.quantity <= 0 or requested.quantity > candidate['shortfall']:
            raise HTTPException(
                status_code=400,
                detail=f"Quantity for {requested.sku} must be between 1 and {candidate['shortfall']}"
            )

        order_items.append({
            'sku': candidate['sku'],
            'name': candidate['name'],
            'category': candidate['category'],
            'quantity': requested.quantity,
            'unit_cost': candidate['unit_cost'],
            'line_total': round(requested.quantity * candidate['unit_cost'], 2),
            'lead_time_days': candidate['lead_time_days'],
        })

    total_value = round(sum(item['line_total'] for item in order_items), 2)
    if total_value > request.budget:
        raise HTTPException(
            status_code=400,
            detail=f"Order total {total_value} exceeds budget {request.budget}"
        )

    # The order arrives when its slowest item does, so use the longest lead time
    lead_time_days = max(item['lead_time_days'] for item in order_items)
    order_date = datetime.now().replace(microsecond=0)

    order = {
        'id': f"RS-{len(submitted_orders) + 1:04d}",
        'items': order_items,
        'total_value': total_value,
        'budget': request.budget,
        'lead_time_days': lead_time_days,
        'order_date': order_date.isoformat(),
        'expected_delivery': (order_date + timedelta(days=lead_time_days)).isoformat(),
        'status': 'Submitted',
    }
    submitted_orders.append(order)
    return order

# Deliberately not /api/orders/submitted: GET /api/orders/{order_id} would capture
# "submitted" as an order id (and 404) depending on route registration order.
@app.get("/api/submitted-orders", response_model=List[SubmittedOrder])
def get_submitted_orders():
    """Restocking orders placed since server start, newest first"""
    return list(reversed(submitted_orders))

@app.get("/api/dashboard/summary")
def get_dashboard_summary(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get summary statistics for dashboard with optional filtering"""
    # Filter inventory
    filtered_inventory = apply_filters(inventory_items, warehouse, category)

    # Filter orders
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)

    total_inventory_value = sum(item["quantity_on_hand"] * item["unit_cost"] for item in filtered_inventory)
    low_stock_items = len([item for item in filtered_inventory if item["quantity_on_hand"] <= item["reorder_point"]])
    pending_orders = len([order for order in filtered_orders if order["status"] in ["Processing", "Backordered"]])
    total_backlog_items = len(backlog_items)

    return {
        "total_inventory_value": round(total_inventory_value, 2),
        "low_stock_items": low_stock_items,
        "pending_orders": pending_orders,
        "total_backlog_items": total_backlog_items,
        "total_orders_value": sum(order["total_value"] for order in filtered_orders)
    }

@app.get("/api/spending/summary")
def get_spending_summary():
    """Get spending summary statistics"""
    return spending_summary

@app.get("/api/spending/monthly")
def get_monthly_spending():
    """Get monthly spending breakdown"""
    return monthly_spending

@app.get("/api/spending/categories")
def get_category_spending():
    """Get spending by category"""
    return category_spending

@app.get("/api/spending/transactions")
def get_recent_transactions():
    """Get recent transactions"""
    return recent_transactions

@app.get("/api/reports/quarterly")
def get_quarterly_reports():
    """Get quarterly performance reports"""
    # Calculate quarterly statistics from orders
    quarters = {}

    for order in orders:
        order_date = order.get('order_date', '')
        # Determine quarter
        if '2025-01' in order_date or '2025-02' in order_date or '2025-03' in order_date:
            quarter = 'Q1-2025'
        elif '2025-04' in order_date or '2025-05' in order_date or '2025-06' in order_date:
            quarter = 'Q2-2025'
        elif '2025-07' in order_date or '2025-08' in order_date or '2025-09' in order_date:
            quarter = 'Q3-2025'
        elif '2025-10' in order_date or '2025-11' in order_date or '2025-12' in order_date:
            quarter = 'Q4-2025'
        else:
            continue

        if quarter not in quarters:
            quarters[quarter] = {
                'quarter': quarter,
                'total_orders': 0,
                'total_revenue': 0,
                'delivered_orders': 0,
                'avg_order_value': 0
            }

        quarters[quarter]['total_orders'] += 1
        quarters[quarter]['total_revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            quarters[quarter]['delivered_orders'] += 1

    # Calculate averages and fulfillment rate
    result = []
    for q, data in quarters.items():
        if data['total_orders'] > 0:
            data['avg_order_value'] = round(data['total_revenue'] / data['total_orders'], 2)
            data['fulfillment_rate'] = round((data['delivered_orders'] / data['total_orders']) * 100, 1)
        result.append(data)

    # Sort by quarter
    result.sort(key=lambda x: x['quarter'])
    return result

@app.get("/api/reports/monthly-trends")
def get_monthly_trends():
    """Get month-over-month trends"""
    months = {}

    for order in orders:
        order_date = order.get('order_date', '')
        if not order_date:
            continue

        # Extract month (format: YYYY-MM-DD)
        month = order_date[:7]  # Gets YYYY-MM

        if month not in months:
            months[month] = {
                'month': month,
                'order_count': 0,
                'revenue': 0,
                'delivered_count': 0
            }

        months[month]['order_count'] += 1
        months[month]['revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            months[month]['delivered_count'] += 1

    # Convert to list and sort
    result = list(months.values())
    result.sort(key=lambda x: x['month'])
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
