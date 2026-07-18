# position-sizing

Traduce un presupuesto de riesgo a una cantidad que el exchange acepte, **sin
pasarse nunca del riesgo**.

```bash
pip install position-sizing
```

## El problema

El cálculo parece trivial —`riesgo / distancia_al_stop`— y no lo es, porque el
resultado hay que hacerlo caber en las restricciones del mercado. Ahí es donde se
pierde dinero, y de dos formas silenciosas.

**Redondear hacia arriba.** Este patrón está en incontables bots:

```python
cantidad = max(1, int(cantidad_teorica / contract_size))
```

Si la cantidad teórica sale `0.1` contratos, `int()` la trunca a `0` y
`max(1, ...)` fuerza **uno**. Acabas de abrir una posición que arriesga **diez
veces** lo autorizado. Con saldo pequeño o stop ancho ocurre siempre, y sin
avisar.

**Ignorar el nocional mínimo.** La orden se rechaza al llegar al exchange, el bot
lo registra como error de red, y nadie se entera de que esa señal nunca se operó.

## Qué hace esta librería

- **Redondea siempre hacia abajo.** Pasarse del presupuesto se paga con dinero;
  quedarse corto solo cuesta rendimiento. El error es asimétrico y el redondeo
  también.
- **Se niega a operar** cuando el mínimo del mercado no cabe en el riesgo — y
  dice **cuánto saldo haría falta** para que cupiera.
- Respeta `contractSize`, escalón de cantidad, mínimos, máximos y nocional mínimo.
- Cuenta **comisiones de ida y vuelta**: con un stop ajustado son la mitad del
  riesgo real.
- Distingue **margen de riesgo**: apalancarse cambia lo que puedes abrir, no lo
  que pierdes si salta el stop.

## Uso

```python
from decimal import Decimal
from position_sizing import MarketSpec, size_for_risk

spec = MarketSpec(
    amount_step=Decimal("0.001"),     # escalón de cantidad
    min_amount=Decimal("0.001"),
    min_notional=Decimal("5"),        # valor mínimo de la orden
)

r = size_for_risk(
    balance=Decimal("1000"),
    risk_fraction=Decimal("0.01"),    # arriesgar el 1%
    entry=Decimal("50000"),
    stop=Decimal("49000"),
    spec=spec,
    fee_rate=Decimal("0.0005"),       # 0,05% por lado
)

if r.ok:
    exchange.create_order(symbol, "market", "buy", float(r.quantity))
else:
    log.warning("sin operar: %s", r.reason)
    # -> "la cantidad mínima del mercado arriesga más de lo presupuestado;
    #     harían falta 5000.00 de saldo"
```

`size_for_risk` devuelve siempre un `Sizing`, nunca `None`:

| Campo | Qué es |
|---|---|
| `quantity` | Cantidad a enviar. Cero si se rechazó |
| `notional` | `precio × cantidad × contract_size` |
| `risk_amount` | Pérdida si salta el stop, comisiones incluidas |
| `risk_budget` | Lo que se pidió arriesgar. `risk_amount` **nunca** lo supera |
| `rejection` | `None`, o el motivo tipado |
| `min_balance_required` | Saldo necesario para superar el rechazo |
| `.ok` / `.reason` | Booleano y explicación lista para un log |

### Con ccxt

```python
from position_sizing import spec_from_ccxt, size_for_risk

spec = spec_from_ccxt(exchange.market("BTC/USDT:USDT"))
r = size_for_risk(balance=bal, risk_fraction=Decimal("0.01"),
                  entry=precio, stop=sl, spec=spec)
```

El adaptador es el **único** punto que sabe de ccxt. Resuelve además una
inconsistencia real: unos exchanges dan `precision.amount` como escalón (`0.001`)
y otros como número de decimales (`3`). Confundirlos hace que toda cantidad salga
mal.

## Dos decisiones de diseño

**`Decimal`, no `float`.** El bug de arriba nace de un truncamiento. Con decimal y
redondeo explícito, la dirección es una decisión y no un accidente del tipo.

**Función pura: recibe una `MarketSpec`, no un exchange.** Se prueba sin red,
sirve para cualquier exchange y —lo que más importa— **calcula igual en backtest
que en producción**. Un backtest que asume posiciones fraccionarias que el mercado
nunca aceptaría infla el retorno; con la misma función en ambos lados, esa mentira
desaparece.

## Alcance

Esto dimensiona órdenes. **No decide cuándo operar, ni dónde poner el stop, ni en
qué dirección.** Es aritmética sobre restricciones de exchange, no una estrategia.

## Licencia

Apache-2.0 · Copyright 2026 Juan Carlos Isaza Arenas
