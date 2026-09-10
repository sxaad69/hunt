"""Pin the trial-proven curve SELL layout (buy16 == sell14 echo rule).

Guards against regressions like the HB2r4H-specific 4Rut3 constant and the
read-only slot15 that both failed on-chain with InvalidBondingCurveV2/6074.
Pure offline — no network, no money.
"""
from solders.pubkey import Pubkey

from hunt.exec.pumpfun.bonding_curve import build_sell_instruction
from hunt.exec.pumpfun.constants import (
    PUMP_CURVE_TRAIL_15,
    PUMP_SELL_DISCRIMINATOR,
)
from hunt.exec.pumpfun.pda import get_bonding_curve_pda, get_bonding_curve_v2_pda

USER = Pubkey.from_string("6xWUNUZZSt8PgYEwzhwcb2h3fER8aTeHvhGWaakAyf6f")
MINT = Pubkey.from_string("Dxr3KQAzVcv8PkhKDUVGoea5Yb3k8jpDdVJvtZQpump")
CURVE = get_bonding_curve_pda(MINT)  # real mmrich curve PDA
CREATOR = Pubkey.from_string("96Z3EfZvWREDbkeGyadnY5GyteXkfwbQwCfW9rrFEh2N")


def test_sell_layout_16_accounts_disc():
    ix = build_sell_instruction(USER, MINT, CURVE, 1000, 1, CREATOR, USER)
    assert len(ix.accounts) == 16
    assert bytes(ix.data)[:8] == PUMP_SELL_DISCRIMINATOR
    assert len(bytes(ix.data)) == 24


def test_sell_slot14_echoes_derived_curve_v2():
    ix = build_sell_instruction(USER, MINT, CURVE, 1000, 1, CREATOR, USER)
    assert ix.accounts[14].pubkey == get_bonding_curve_v2_pda(MINT)
    assert ix.accounts[14].pubkey == Pubkey.from_string(
        "6CQB7VhbQtADbxxV7kgwchkK3iqievMREwgXGYSAyq2e")  # mmrich trial value
    assert not ix.accounts[14].is_writable


def test_sell_slot15_vault_writable():
    ix = build_sell_instruction(USER, MINT, CURVE, 1000, 1, CREATOR, USER)
    assert ix.accounts[15].pubkey == PUMP_CURVE_TRAIL_15
    assert ix.accounts[15].is_writable
    assert ix.accounts[6].pubkey == USER and ix.accounts[6].is_signer
