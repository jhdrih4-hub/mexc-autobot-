from bot.exchange import LEV_PAT, STABLE_BASES


def test_lev_pattern():
    assert LEV_PAT.search("BTC3L")
    assert LEV_PAT.search("ETH5S")
    assert LEV_PAT.search("XYZUP")
    assert not LEV_PAT.search("BTC")


def test_stables():
    assert "USDC" in STABLE_BASES
