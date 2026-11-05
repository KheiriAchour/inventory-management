<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else-if="candidates.length === 0" class="card empty-state">
      {{ t('restocking.noCandidates') }}
    </div>
    <div v-else>
      <div class="card budget-card">
        <div class="budget-header">
          <label for="budget-slider" class="budget-label">{{ t('restocking.budgetSlider') }}</label>
          <span class="budget-value">{{ displayCurrency(budget) }}</span>
        </div>
        <input
          id="budget-slider"
          class="budget-slider"
          type="range"
          min="0"
          :max="sliderMax"
          :step="sliderStep"
          v-model.number="budget"
        />
        <div class="budget-scale">
          <span>{{ displayCurrency(0) }}</span>
          <span>{{ displayCurrency(sliderMax) }}</span>
        </div>
      </div>

      <div class="stats-grid">
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.budget') }}</div>
          <div class="stat-value">{{ displayCurrency(budget) }}</div>
        </div>
        <div :class="['stat-card', overBudget ? 'danger' : 'warning']">
          <div class="stat-label">{{ t('restocking.plannedSpend') }}</div>
          <div class="stat-value">{{ displayCurrency(plannedSpend) }}</div>
        </div>
        <div :class="['stat-card', overBudget ? 'danger' : 'success']">
          <div class="stat-label">{{ t('restocking.remainingBudget') }}</div>
          <div class="stat-value">{{ displayCurrency(remaining) }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-label">{{ t('restocking.leadTime') }}</div>
          <div class="stat-value">{{ t('restocking.days', { days: leadTime }) }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendedItems') }} ({{ recommended.length }})</h3>
        </div>

        <div v-if="recommended.length === 0" class="empty-state">
          {{ t('restocking.noRecommendations') }}
        </div>
        <div v-else class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('restocking.table.sku') }}</th>
                <th>{{ t('restocking.table.name') }}</th>
                <th>{{ t('restocking.table.category') }}</th>
                <th>{{ t('restocking.table.trend') }}</th>
                <th>{{ t('restocking.table.shortfall') }}</th>
                <th>{{ t('restocking.table.quantity') }}</th>
                <th>{{ t('restocking.table.unitCost') }}</th>
                <th>{{ t('restocking.table.lineTotal') }}</th>
                <th>{{ t('restocking.table.leadTime') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in recommended" :key="row.sku">
                <td><strong>{{ row.sku }}</strong></td>
                <td>{{ translateProductName(row.name) }}</td>
                <td>{{ row.category }}</td>
                <td>
                  <span :class="['badge', row.trend]">{{ t(`trends.${row.trend}`) }}</span>
                </td>
                <td>{{ row.shortfall }}</td>
                <td>
                  <input
                    class="qty-input"
                    type="number"
                    min="0"
                    :max="row.shortfall"
                    step="1"
                    :value="row.quantity"
                    @input="setQuantity(row, $event)"
                  />
                </td>
                <td>{{ displayCurrency(row.unit_cost) }}</td>
                <td><strong>{{ displayCurrency(row.quantity * row.unit_cost) }}</strong></td>
                <td>{{ t('restocking.days', { days: row.lead_time_days }) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-if="overBudget" class="warning-banner">{{ t('restocking.overBudget') }}</div>
      <div v-if="orderError" class="error">{{ orderError }}</div>
      <div v-if="submittedOrder" class="success-banner">
        {{ t('restocking.orderSuccess', { id: submittedOrder.id, days: submittedOrder.lead_time_days }) }}
        <router-link to="/orders">{{ t('restocking.viewOrders') }}</router-link>
      </div>

      <div class="actions">
        <button class="place-order-btn" :disabled="!canPlaceOrder" @click="placeOrder">
          {{ submitting ? t('restocking.submitting') : t('restocking.placeOrder') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrency } from '../utils/currency'

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, translateProductName } = useI18n()

    const loading = ref(true)
    const error = ref(null)
    const candidates = ref([])
    const maxBudget = ref(0)
    const budget = ref(0)
    // Per-sku manual quantity edits; cleared whenever the budget changes
    const overrides = ref({})
    const submitting = ref(false)
    const submittedOrder = ref(null)
    const orderError = ref(null)

    // Amounts are USD internally; formatCurrency converts to JPY when needed
    const displayCurrency = (usd) => formatCurrency(usd, currentCurrency.value)

    // Slider step in USD. Browsers clamp to max, so max stays reachable even when
    // it is not a multiple of the step.
    const sliderStep = computed(() => (maxBudget.value >= 1000 ? 100 : 10))
    // Round max up to a step multiple so the thumb can actually reach the end
    const sliderMax = computed(() => Math.ceil(maxBudget.value / sliderStep.value) * sliderStep.value)

    const trendRank = (trend) => (trend === 'increasing' ? 0 : 1)

    // Greedy fill: largest shortfall first (increasing trend wins ties)
    const baseRecommendations = computed(() => {
      const sorted = [...candidates.value].sort((a, b) =>
        b.shortfall - a.shortfall || trendRank(a.trend) - trendRank(b.trend)
      )
      let remainingBudget = budget.value
      const result = []
      for (const item of sorted) {
        const qty = Math.min(item.shortfall, Math.floor(remainingBudget / item.unit_cost))
        if (qty <= 0) continue
        remainingBudget -= qty * item.unit_cost
        result.push({ ...item, quantity: qty })
      }
      return result
    })

    // Apply manual overrides on top of the greedy suggestion
    const recommended = computed(() =>
      baseRecommendations.value.map((item) => {
        const override = overrides.value[item.sku]
        return override === undefined ? item : { ...item, quantity: override }
      })
    )

    const includedItems = computed(() => recommended.value.filter((i) => i.quantity > 0))
    const plannedSpend = computed(() =>
      includedItems.value.reduce((sum, i) => sum + i.quantity * i.unit_cost, 0)
    )
    const remaining = computed(() => budget.value - plannedSpend.value)
    // Small epsilon guards against floating point error on decimal unit costs
    const overBudget = computed(() => plannedSpend.value > budget.value + 1e-6)
    const leadTime = computed(() =>
      includedItems.value.reduce((max, i) => Math.max(max, i.lead_time_days), 0)
    )
    const canPlaceOrder = computed(
      () => !submitting.value && includedItems.value.length > 0 && !overBudget.value
    )

    const setQuantity = (row, event) => {
      let value = Math.floor(Number(event.target.value))
      if (!Number.isFinite(value)) value = 0
      value = Math.min(Math.max(value, 0), row.shortfall)
      overrides.value = { ...overrides.value, [row.sku]: value }
      // Reflect the clamped value in the input
      event.target.value = value
      submittedOrder.value = null
    }

    watch(budget, () => {
      overrides.value = {}
      submittedOrder.value = null
      orderError.value = null
    })

    const loadCandidates = async () => {
      try {
        loading.value = true
        error.value = null
        const data = await api.getRestockingCandidates()
        candidates.value = data.items
        maxBudget.value = Math.ceil(data.max_budget)
        // Default to roughly half, snapped to the slider step
        const step = sliderStep.value
        budget.value = Math.min(sliderMax.value, Math.round(maxBudget.value / 2 / step) * step)
      } catch (err) {
        error.value = t('restocking.loadError') + ': ' + err.message
      } finally {
        loading.value = false
      }
    }

    const placeOrder = async () => {
      if (!canPlaceOrder.value) return
      submitting.value = true
      orderError.value = null
      submittedOrder.value = null
      try {
        submittedOrder.value = await api.createRestockingOrder({
          budget: budget.value,
          items: includedItems.value.map((i) => ({ sku: i.sku, quantity: i.quantity }))
        })
      } catch (err) {
        orderError.value = err.response?.data?.detail || t('restocking.orderFailed')
      } finally {
        submitting.value = false
      }
    }

    onMounted(loadCandidates)

    return {
      t,
      loading,
      error,
      candidates,
      budget,
      sliderStep,
      sliderMax,
      recommended,
      plannedSpend,
      remaining,
      overBudget,
      leadTime,
      canPlaceOrder,
      submitting,
      submittedOrder,
      orderError,
      displayCurrency,
      setQuantity,
      placeOrder,
      translateProductName
    }
  }
}
</script>

<style scoped>
.budget-card {
  padding: 1.5rem;
  margin-bottom: 1.5rem;
}

.budget-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 1rem;
}

.budget-label {
  font-size: 0.875rem;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.budget-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: #0f172a;
}

.budget-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 6px;
  background: #e2e8f0;
  border-radius: 3px;
  outline: none;
  cursor: pointer;
}

.budget-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: #0f172a;
  border: 2px solid white;
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.4);
}

.budget-slider::-moz-range-thumb {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #0f172a;
  border: 2px solid white;
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.4);
}

.budget-slider:focus-visible {
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.4);
}

.budget-scale {
  display: flex;
  justify-content: space-between;
  margin-top: 0.5rem;
  font-size: 0.813rem;
  color: #64748b;
}

.qty-input {
  width: 80px;
  padding: 0.375rem 0.5rem;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  color: #0f172a;
  font-size: 0.875rem;
}

.empty-state {
  padding: 2rem;
  text-align: center;
  color: #64748b;
}

.warning-banner,
.success-banner {
  padding: 0.75rem 1rem;
  border-radius: 8px;
  margin: 1rem 0;
  font-size: 0.875rem;
}

.warning-banner {
  background: #fef3c7;
  color: #92400e;
  border: 1px solid #fde68a;
}

.success-banner {
  background: #d1fae5;
  color: #065f46;
  border: 1px solid #a7f3d0;
}

.success-banner a {
  margin-left: 0.75rem;
  color: #065f46;
  font-weight: 600;
  text-decoration: underline;
}

.actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 1rem;
}

.place-order-btn {
  padding: 0.625rem 1.5rem;
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 8px;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
}

.place-order-btn:hover:not(:disabled) {
  background: #1e293b;
}

.place-order-btn:disabled {
  background: #94a3b8;
  cursor: not-allowed;
}
</style>
