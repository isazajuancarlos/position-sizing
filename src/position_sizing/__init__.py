# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Juan Carlos Isaza Arenas

"""position-sizing — traduce un presupuesto de riesgo a una cantidad que el
exchange acepte, sin pasarse nunca del riesgo.

El cálculo parece trivial —`riesgo / distancia_al_stop`— y no lo es, porque el
resultado hay que hacerlo caber en las restricciones del mercado: tamaño de
contrato, escalón de cantidad, mínimos y nocional mínimo. Ahí es donde se pierde
dinero de verdad, y de dos formas silenciosas:

**Redondear hacia arriba.** El caso clásico:

    cantidad = max(1, int(cantidad_teorica / contract_size))

Si la cantidad teórica sale 0,3 contratos, `int()` la trunca a 0 y `max(1, ...)`
fuerza **uno**. Se acaba de abrir una posición que arriesga **3,3 veces** lo
autorizado. Con saldo pequeño o stop ancho ocurre siempre, y en silencio.

**Ignorar el nocional mínimo.** La orden se rechaza al llegar al exchange, el
bot lo registra como error de red y se sigue.

Esta librería redondea **siempre hacia abajo** y, cuando el mínimo del mercado no
cabe en el presupuesto de riesgo, **se niega a operar** y dice cuánto saldo haría
falta para que cupiera.

## Dos decisiones de diseño

**`Decimal`, no `float`.** El bug de arriba nace de un truncamiento. Con decimal
y redondeo explícito la dirección es una decisión, no un accidente del tipo.

**Función pura: recibe una `MarketSpec`, no un exchange.** Se prueba sin red,
sirve para cualquier exchange y —lo que más importa— **calcula igual en backtest
que en producción**. Un backtest que asume posiciones fraccionarias que el
mercado nunca aceptaría infla el retorno; con la misma función en los dos lados,
esa mentira desaparece.

Para ccxt hay un adaptador de una línea: :func:`spec_from_ccxt`.

```python
from decimal import Decimal
from position_sizing import MarketSpec, size_for_risk

spec = MarketSpec(amount_step=Decimal("0.001"), min_amount=Decimal("0.001"),
                  min_notional=Decimal("5"))

r = size_for_risk(balance=Decimal("1000"), risk_fraction=Decimal("0.01"),
                  entry=Decimal("50000"), stop=Decimal("49000"), spec=spec)

if r.ok:
    exchange.create_order(sym, "market", "buy", float(r.quantity))
else:
    log.warning("sin operar: %s", r.reason)
```
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from enum import Enum
from typing import Any, Mapping, Optional

__all__ = [
    "MarketSpec",
    "Sizing",
    "Rejection",
    "size_for_risk",
    "spec_from_ccxt",
]

_ZERO = Decimal(0)


class Rejection(str, Enum):
    """Por qué no se pudo dimensionar. Nunca se devuelve una cantidad «casi
    buena»: o cabe en el riesgo, o se explica por qué no."""

    #: El presupuesto de riesgo, la distancia al stop o el saldo no son positivos.
    BAD_INPUT = "bad_input"
    #: Tras redondear hacia abajo al escalón del mercado, la cantidad es cero.
    BELOW_STEP = "below_step"
    #: La cantidad mínima del mercado arriesga MÁS de lo presupuestado.
    MIN_AMOUNT_EXCEEDS_RISK = "min_amount_exceeds_risk"
    #: El nocional mínimo del mercado arriesga MÁS de lo presupuestado.
    MIN_NOTIONAL_EXCEEDS_RISK = "min_notional_exceeds_risk"
    #: No hay saldo para pagar la posición (spot) o su margen (derivados).
    INSUFFICIENT_BALANCE = "insufficient_balance"


@dataclass(frozen=True)
class MarketSpec:
    """Restricciones del mercado. Datos, no un cliente de exchange.

    Todo es opcional salvo `amount_step`: un mercado sin mínimos declarados es
    perfectamente válido.
    """

    #: Escalón de cantidad. La cantidad final es múltiplo exacto de este valor.
    amount_step: Decimal
    #: Unidades de subyacente por contrato. 1 en spot y en derivados lineales
    #: cotizados en moneda base.
    contract_size: Decimal = Decimal(1)
    #: Cantidad mínima admitida, en las mismas unidades que `amount_step`.
    min_amount: Optional[Decimal] = None
    #: Cantidad máxima admitida.
    max_amount: Optional[Decimal] = None
    #: Valor mínimo de la orden (precio x cantidad x contract_size).
    min_notional: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.amount_step <= _ZERO:
            raise ValueError("amount_step debe ser positivo")
        if self.contract_size <= _ZERO:
            raise ValueError("contract_size debe ser positivo")


@dataclass(frozen=True)
class Sizing:
    """Resultado del dimensionamiento.

    Se devuelve siempre un `Sizing`, no una cantidad suelta: quien llama necesita
    poder registrar por qué el riesgo real difiere del pedido, o por qué no se
    operó. Un `None` no permite eso.
    """

    #: Cantidad a enviar al exchange. Cero si se rechazó.
    quantity: Decimal
    #: Valor de la orden: precio x cantidad x contract_size.
    notional: Decimal
    #: Pérdida si salta el stop, incluidas comisiones de ida y vuelta.
    risk_amount: Decimal
    #: Riesgo que se pidió arriesgar. `risk_amount` nunca lo supera.
    risk_budget: Decimal
    #: `None` si se dimensionó; el motivo si no.
    rejection: Optional[Rejection] = None
    #: Saldo que haría falta para superar el rechazo, cuando se puede calcular.
    min_balance_required: Optional[Decimal] = None

    @property
    def ok(self) -> bool:
        """`True` si hay una cantidad utilizable."""
        return self.rejection is None and self.quantity > _ZERO

    @property
    def reason(self) -> str:
        """Explicación legible, pensada para ir directa a un log."""
        if self.ok:
            pct = (self.risk_amount / self.risk_budget * 100) if self.risk_budget else _ZERO
            return (f"cantidad {self.quantity} · nocional {self.notional} · "
                    f"riesgo {self.risk_amount} ({pct:.1f}% del presupuesto)")
        base = {
            Rejection.BAD_INPUT: "entradas inválidas (riesgo, stop o saldo no positivos)",
            Rejection.BELOW_STEP: "la cantidad calculada no alcanza el escalón mínimo del mercado",
            Rejection.MIN_AMOUNT_EXCEEDS_RISK: "la cantidad mínima del mercado arriesga más de lo presupuestado",
            Rejection.MIN_NOTIONAL_EXCEEDS_RISK: "el nocional mínimo del mercado arriesga más de lo presupuestado",
            Rejection.INSUFFICIENT_BALANCE: "saldo insuficiente para la posición",
        }.get(self.rejection, str(self.rejection))
        if self.min_balance_required is not None:
            # Se muestra con dos decimales por ser una cifra de dinero: el valor
            # crudo puede salir como "5000" o como "5000.0000000", según de qué
            # operación decimal venga, y ninguna de las dos se lee como un
            # importe. El campo `min_balance_required` conserva la precisión
            # completa; esto solo afecta al texto del log.
            return f"{base}; harían falta {self.min_balance_required:.2f} de saldo"
        return base

    def __bool__(self) -> bool:
        return self.ok


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Redondea HACIA ABAJO al múltiplo del escalón.

    Siempre hacia abajo, nunca al más cercano: pasarse del presupuesto de riesgo
    es un error asimétrico —el exceso se paga con dinero real— mientras que
    quedarse corto solo cuesta rendimiento.
    """
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _rejected(rej: Rejection, budget: Decimal,
              min_balance: Optional[Decimal] = None) -> Sizing:
    return Sizing(quantity=_ZERO, notional=_ZERO, risk_amount=_ZERO,
                  risk_budget=budget, rejection=rej, min_balance_required=min_balance)


def size_for_risk(
    *,
    balance: Decimal,
    risk_fraction: Decimal,
    entry: Decimal,
    stop: Decimal,
    spec: MarketSpec,
    fee_rate: Decimal = _ZERO,
    leverage: Decimal = Decimal(1),
) -> Sizing:
    """Traduce un presupuesto de riesgo a una cantidad que el mercado acepte.

    :param balance: Saldo disponible en moneda de cotización.
    :param risk_fraction: Fracción del saldo a arriesgar (``0.01`` = 1%).
    :param entry: Precio de entrada previsto.
    :param stop: Precio del stop. Su lado respecto a `entry` decide la dirección;
        solo importa la distancia.
    :param spec: Restricciones del mercado.
    :param fee_rate: Comisión POR LADO (``0.0005`` = 0,05%). Se cuenta ida y
        vuelta: una comisión ignorada convierte un stop al ras en pérdida mayor
        que la presupuestada.
    :param leverage: Apalancamiento. Solo afecta al margen requerido, nunca al
        riesgo: apalancarse no cambia cuánto se pierde si salta el stop.

    :returns: Un :class:`Sizing`. Comprueba ``.ok`` antes de usar ``.quantity``.
    """
    distance = abs(entry - stop)
    if balance <= _ZERO or risk_fraction <= _ZERO or distance <= _ZERO or entry <= _ZERO:
        return _rejected(Rejection.BAD_INPUT, _ZERO)

    budget = balance * risk_fraction

    # Riesgo por unidad de subyacente: lo que se pierde en el stop MÁS lo que
    # cuesta entrar y salir. Sin el segundo término, un stop ajustado arriesga
    # sistemáticamente más de lo presupuestado.
    risk_per_unit = distance + (entry + stop) * fee_rate
    if risk_per_unit <= _ZERO:
        return _rejected(Rejection.BAD_INPUT, budget)

    units = budget / risk_per_unit                 # unidades de subyacente
    raw = units / spec.contract_size               # cantidad en unidades de mercado
    qty = _floor_to_step(raw, spec.amount_step)

    if spec.max_amount is not None and qty > spec.max_amount:
        qty = _floor_to_step(spec.max_amount, spec.amount_step)

    if qty <= _ZERO:
        # Cuánto saldo haría falta para que el escalón mínimo cupiera en el
        # riesgo. Es lo accionable: no "no se puede", sino "faltan X".
        floor_qty = spec.min_amount if spec.min_amount is not None else spec.amount_step
        needed = (floor_qty * spec.contract_size * risk_per_unit) / risk_fraction
        rej = (Rejection.MIN_AMOUNT_EXCEEDS_RISK if spec.min_amount is not None
               else Rejection.BELOW_STEP)
        return _rejected(rej, budget, needed.quantize(entry, rounding=ROUND_UP))

    if spec.min_amount is not None and qty < spec.min_amount:
        # El mínimo del mercado existe pero arriesga más de lo presupuestado.
        # Aquí es donde el código ingenuo abre igualmente. No se abre.
        needed = (spec.min_amount * spec.contract_size * risk_per_unit) / risk_fraction
        return _rejected(Rejection.MIN_AMOUNT_EXCEEDS_RISK, budget,
                         needed.quantize(entry, rounding=ROUND_UP))

    notional = qty * spec.contract_size * entry

    if spec.min_notional is not None and notional < spec.min_notional:
        units_needed = spec.min_notional / entry
        needed = (units_needed * risk_per_unit) / risk_fraction
        return _rejected(Rejection.MIN_NOTIONAL_EXCEEDS_RISK, budget,
                         needed.quantize(entry, rounding=ROUND_UP))

    # Margen: con apalancamiento 1 es el nocional entero. Las comisiones de
    # entrada también salen del saldo.
    margin = notional / leverage + notional * fee_rate
    if margin > balance:
        affordable_units = (balance / (entry / leverage + entry * fee_rate))
        qty = _floor_to_step(affordable_units / spec.contract_size, spec.amount_step)
        if qty <= _ZERO or (spec.min_amount is not None and qty < spec.min_amount):
            return _rejected(Rejection.INSUFFICIENT_BALANCE, budget)
        notional = qty * spec.contract_size * entry

    risk_amount = qty * spec.contract_size * risk_per_unit
    return Sizing(quantity=qty, notional=notional, risk_amount=risk_amount,
                  risk_budget=budget)


def spec_from_ccxt(market: Mapping[str, Any]) -> MarketSpec:
    """Construye una :class:`MarketSpec` desde un mercado de ccxt.

    Es el único punto que sabe de ccxt, y es deliberado: el cálculo queda puro y
    probable sin red, y sirve igual para otro exchange o para un backtest.

    :param market: El diccionario de ``exchange.market(symbol)``.
    """
    limits = market.get("limits") or {}
    amount = limits.get("amount") or {}
    cost = limits.get("cost") or {}
    precision = market.get("precision") or {}

    def _dec(v: Any) -> Optional[Decimal]:
        if v is None:
            return None
        return Decimal(str(v))

    # ccxt expresa la precisión de dos formas según el exchange: como escalón
    # (0.001) o como número de decimales (3). Se distinguen porque un escalón
    # nunca es un entero >= 1.
    step = _dec(precision.get("amount"))
    if step is None:
        step = _dec(amount.get("min")) or Decimal("1")
    elif step >= 1 and step == step.to_integral_value():
        step = Decimal(1).scaleb(-int(step))

    return MarketSpec(
        amount_step=step,
        contract_size=_dec(market.get("contractSize")) or Decimal(1),
        min_amount=_dec(amount.get("min")),
        max_amount=_dec(amount.get("max")),
        min_notional=_dec(cost.get("min")),
    )
