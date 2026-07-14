from src.report import summarize


def test_summarize(tmp_path):
    p = tmp_path / "trades.csv"
    p.write_text(
        "ts,kind,ticker,signed_count,price,fee,pos_after,realized_delta\n"
        "1.0,fill,A,10,0.4000,0.10,10,0.00\n"
        "2.0,fill,A,-4,0.6000,0.05,6,0.80\n"
        "3.0,settle,A,-6,1.0000,0.00,0,3.60\n"
        "4.0,fill,B,-5,0.5000,0.09,-5,0.00\n"
    )
    per, total = summarize(str(p))
    assert per["A"].fills == 2
    assert per["A"].contracts == 14
    assert per["A"].settles == 1
    assert per["A"].gross == 4.40
    assert round(per["A"].net, 2) == 4.25
    assert per["B"].settles == 0
    assert round(total.net, 2) == 4.25 - 0.09
    assert total.fills == 3
