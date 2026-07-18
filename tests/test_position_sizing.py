# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Juan Carlos Isaza Arenas

"""Pruebas de position-sizing.

La primera clase es la que justifica que la librería exista: reproduce el bug
real encontrado en un bot en producción, y comprueba que aquí no ocurre.
"""

from decimal import Decimal as D

import pytest

from position_sizing import (
    MarketSpec,
    Rejection,
    size_for_risk,
    spec_from_ccxt,
)


# --- El bug que motiva la librería -------------------------------------------

class TestNuncaSePasaDelRiesgo:
    """`max(1, int(cantidad / contract_size))` es el patrón que se repite en
    incontables bots, y arriesga de más en silencio."""

    def test_reproduce_el_bug_del_codigo_ingenuo(self):
        # Caso real: saldo pequeño, stop ancho, contratos grandes.
        balance, riesgo, entry, stop = D("500"), D("0.01"), D("50000"), D("45000")
        contract_size = D("0.01")

        presupuesto = balance * riesgo                      # $5
        unidades = presupuesto / abs(entry - stop)          # 0.001 BTC
        teorica = unidades / contract_size                  # 0.1 contratos

        # Lo que hace el código ingenuo:
        ingenua = max(1, int(teorica))                      # -> 1 contrato
        riesgo_ingenuo = ingenua * contract_size * abs(entry - stop)
        assert riesgo_ingenuo == D("50")                    # ¡10x el presupuesto!
        assert riesgo_ingenuo > presupuesto * 9

        # Lo que hace la librería: se niega, y dice cuánto haría falta.
        r = size_for_risk(balance=balance, risk_fraction=riesgo, entry=entry,
                          stop=stop, spec=MarketSpec(amount_step=D("1"),
                                                     contract_size=contract_size,
                                                     min_amount=D("1")))
        assert not r.ok
        assert r.rejection is Rejection.MIN_AMOUNT_EXCEEDS_RISK
        assert r.min_balance_required is not None
        assert r.min_balance_required >= D("5000")   # 1 contrato = $50 de riesgo al 1%

    @pytest.mark.parametrize("balance,entry,stop", [
        ("1000", "50000", "49000"), ("250", "3000", "2850"),
        ("10000", "100", "97"), ("75", "1.5", "1.4"),
    ])
    def test_el_riesgo_real_nunca_supera_el_presupuesto(self, balance, entry, stop):
        spec = MarketSpec(amount_step=D("0.0001"), min_amount=D("0.0001"))
        r = size_for_risk(balance=D(balance), risk_fraction=D("0.01"),
                          entry=D(entry), stop=D(stop), spec=spec)
        if r.ok:
            assert r.risk_amount <= r.risk_budget, "se pasó del presupuesto"

    def test_redondea_hacia_abajo_nunca_al_mas_cercano(self):
        # Cantidad teórica 1.9 escalones: al más cercano daría 2, y eso excede.
        spec = MarketSpec(amount_step=D("1"), min_amount=D("1"))
        r = size_for_risk(balance=D("1900"), risk_fraction=D("0.01"),
                          entry=D("100"), stop=D("90"), spec=spec)
        assert r.ok
        assert r.quantity == D("1")          # no 2
        assert r.risk_amount <= r.risk_budget


# --- Restricciones del mercado -----------------------------------------------

class TestRestriccionesDelMercado:

    def test_respeta_el_escalon_de_cantidad(self):
        spec = MarketSpec(amount_step=D("0.001"))
        r = size_for_risk(balance=D("10000"), risk_fraction=D("0.01"),
                          entry=D("2000"), stop=D("1900"), spec=spec)
        assert r.ok
        assert r.quantity % D("0.001") == 0

    def test_rechaza_si_el_nocional_minimo_no_cabe_en_el_riesgo(self):
        spec = MarketSpec(amount_step=D("0.0001"), min_notional=D("10000"))
        r = size_for_risk(balance=D("100"), risk_fraction=D("0.01"),
                          entry=D("50000"), stop=D("49000"), spec=spec)
        assert not r.ok
        assert r.rejection is Rejection.MIN_NOTIONAL_EXCEEDS_RISK
        assert r.min_balance_required > D("100")

    def test_respeta_la_cantidad_maxima(self):
        spec = MarketSpec(amount_step=D("1"), max_amount=D("5"))
        r = size_for_risk(balance=D("1000000"), risk_fraction=D("0.01"),
                          entry=D("100"), stop=D("99"), spec=spec)
        assert r.ok
        assert r.quantity == D("5")

    def test_contract_size_distinto_de_uno(self):
        # 1 contrato = 0.01 unidades: la cantidad va en contratos, el riesgo en
        # unidades. Confundirlos es el otro error clásico.
        spec = MarketSpec(amount_step=D("1"), contract_size=D("0.01"), min_amount=D("1"))
        r = size_for_risk(balance=D("100000"), risk_fraction=D("0.01"),
                          entry=D("50000"), stop=D("49000"), spec=spec)
        assert r.ok
        assert r.risk_amount <= r.risk_budget
        assert r.notional == r.quantity * D("0.01") * D("50000")


# --- Comisiones ---------------------------------------------------------------

class TestComisiones:

    def test_la_comision_reduce_la_cantidad(self):
        spec = MarketSpec(amount_step=D("0.00000001"))
        args = dict(balance=D("10000"), risk_fraction=D("0.01"),
                    entry=D("100"), stop=D("99"), spec=spec)
        sin = size_for_risk(**args)
        con = size_for_risk(**args, fee_rate=D("0.001"))
        assert con.quantity < sin.quantity, "ignorar comisiones sobredimensiona"

    def test_con_stop_al_ras_la_comision_domina(self):
        # Stop a 0.1%: con comisión de 0.05% por lado, las comisiones casi
        # igualan al stop, así que el riesgo real es ~2x. Un bot que las ignore
        # arriesga el doble de lo que cree.
        #
        # Hace falta apalancamiento para aislar el efecto: sin él, una posición
        # con stop tan ajustado excede el saldo y manda la restricción de saldo,
        # no la de comisión (ver TestSaldoYApalancamiento).
        spec = MarketSpec(amount_step=D("0.00000001"))
        args = dict(balance=D("10000"), risk_fraction=D("0.01"),
                    entry=D("1000"), stop=D("999"), spec=spec, leverage=D("20"))
        sin = size_for_risk(**args)
        con = size_for_risk(**args, fee_rate=D("0.0005"))
        assert con.quantity < sin.quantity / D("1.9")
        assert con.risk_amount <= con.risk_budget


# --- Saldo y apalancamiento ---------------------------------------------------

class TestSaldoYApalancamiento:

    def test_no_compra_mas_de_lo_que_alcanza_el_saldo(self):
        # Stop muy ajustado: el riesgo permite mucha cantidad, el saldo no.
        spec = MarketSpec(amount_step=D("0.0001"))
        r = size_for_risk(balance=D("100"), risk_fraction=D("0.5"),
                          entry=D("50000"), stop=D("49999"), spec=spec)
        if r.ok:
            assert r.notional <= D("100"), "gastó más saldo del que hay"

    def test_cuando_manda_el_saldo_el_riesgo_queda_por_debajo(self):
        # Descubierto al fallar una prueba: con stop muy ajustado, el riesgo
        # permite una posición que vale varias veces el saldo. Ahí manda el
        # saldo, y el riesgo real queda POR DEBAJO del presupuesto — nunca por
        # encima. Se fija para que un cambio futuro no lo invierta.
        spec = MarketSpec(amount_step=D("0.00000001"))
        r = size_for_risk(balance=D("10000"), risk_fraction=D("0.01"),
                          entry=D("1000"), stop=D("999"), spec=spec)
        assert r.ok
        assert r.notional <= D("10000")
        assert r.risk_amount <= r.risk_budget

    def test_el_apalancamiento_no_cambia_el_riesgo(self):
        # Apalancarse permite mayor nocional, pero si salta el stop se pierde lo
        # mismo. Confundir margen con riesgo es cómo se liquidan las cuentas.
        spec = MarketSpec(amount_step=D("0.0001"))
        args = dict(balance=D("1000"), risk_fraction=D("0.01"),
                    entry=D("100"), stop=D("95"), spec=spec)
        r1 = size_for_risk(**args, leverage=D("1"))
        r10 = size_for_risk(**args, leverage=D("10"))
        assert r1.quantity == r10.quantity
        assert r1.risk_amount == r10.risk_amount


# --- Entradas inválidas -------------------------------------------------------

class TestEntradasInvalidas:

    @pytest.mark.parametrize("kw", [
        {"balance": D("0")}, {"balance": D("-100")}, {"risk_fraction": D("0")},
        {"entry": D("0")}, {"stop": D("100")},   # stop == entry -> distancia 0
    ])
    def test_rechaza_sin_reventar(self, kw):
        base = dict(balance=D("1000"), risk_fraction=D("0.01"),
                    entry=D("100"), stop=D("95"),
                    spec=MarketSpec(amount_step=D("0.001")))
        base.update(kw)
        r = size_for_risk(**base)
        assert not r.ok
        assert r.rejection is Rejection.BAD_INPUT

    def test_spec_invalida_falla_al_construirse(self):
        with pytest.raises(ValueError):
            MarketSpec(amount_step=D("0"))
        with pytest.raises(ValueError):
            MarketSpec(amount_step=D("1"), contract_size=D("-1"))


# --- El resultado se explica solo ---------------------------------------------

class TestResultado:

    def test_es_evaluable_como_booleano(self):
        spec = MarketSpec(amount_step=D("0.001"))
        assert size_for_risk(balance=D("10000"), risk_fraction=D("0.01"),
                             entry=D("100"), stop=D("95"), spec=spec)
        assert not size_for_risk(balance=D("0"), risk_fraction=D("0.01"),
                                 entry=D("100"), stop=D("95"), spec=spec)

    def test_el_motivo_sirve_para_un_log(self):
        spec = MarketSpec(amount_step=D("1"), min_amount=D("1000"))
        r = size_for_risk(balance=D("100"), risk_fraction=D("0.01"),
                          entry=D("100"), stop=D("90"), spec=spec)
        assert not r.ok
        assert "arriesga más" in r.reason
        assert "harían falta" in r.reason

    def test_el_saldo_necesario_se_lee_como_dinero(self):
        # El texto va a un log que lee una persona, y "5000" o "5000.0000000"
        # no se leen como un importe. Se fija el formato porque el README y el
        # artículo publicado citan esta salida literalmente.
        spec = MarketSpec(amount_step=D("1"), contract_size=D("0.01"), min_amount=D("1"))
        r = size_for_risk(balance=D("500"), risk_fraction=D("0.01"),
                          entry=D("50000"), stop=D("45000"), spec=spec)
        assert not r.ok
        assert "harían falta 5000.00 de saldo" in r.reason

    def test_al_dimensionar_el_motivo_trae_las_cifras(self):
        spec = MarketSpec(amount_step=D("0.001"))
        r = size_for_risk(balance=D("10000"), risk_fraction=D("0.01"),
                          entry=D("100"), stop=D("95"), spec=spec)
        assert r.ok
        for t in ("cantidad", "nocional", "riesgo", "presupuesto"):
            assert t in r.reason


# --- Adaptador de ccxt --------------------------------------------------------

class TestAdaptadorCcxt:

    def test_precision_como_escalon(self):
        spec = spec_from_ccxt({"precision": {"amount": 0.001},
                               "limits": {"amount": {"min": 0.001, "max": 1000},
                                          "cost": {"min": 5}}})
        assert spec.amount_step == D("0.001")
        assert spec.min_amount == D("0.001")
        assert spec.min_notional == D("5")

    def test_precision_como_numero_de_decimales(self):
        # Algunos exchanges devuelven 3 en vez de 0.001. Sin distinguirlo, el
        # escalón sería 3 unidades enteras y toda cantidad quedaría mal.
        spec = spec_from_ccxt({"precision": {"amount": 3}, "limits": {}})
        assert spec.amount_step == D("0.001")

    def test_mercado_minimo_no_revienta(self):
        spec = spec_from_ccxt({})
        assert spec.amount_step > 0
        assert spec.contract_size == D("1")

    def test_toma_el_contract_size(self):
        spec = spec_from_ccxt({"contractSize": 0.01, "precision": {"amount": 1},
                               "limits": {}})
        assert spec.contract_size == D("0.01")
