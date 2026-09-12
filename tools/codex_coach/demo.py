#!/usr/bin/env python3
"""Render an inspectable demonstration from actual archived coaching records."""
from datetime import datetime
import html
import json
from pathlib import Path
import sqlite3

import markdown
import coach



def md(text):
    return markdown.markdown(html.escape(text),extensions=['fenced_code','tables'])


def build():
    with sqlite3.connect((coach.STATE/'digest.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute("SELECT payload FROM digests WHERE kind='daily' ORDER BY period_start,id").fetchall()
        if not rows:raise SystemExit('No archived daily digest')
        d=json.loads(rows[-1]['payload'])
        receipts={}
        for row in db.execute('SELECT target,status,attempts,receipt FROM deliveries WHERE digest_id=?',(d['id'],)):
            receipts[row['target']]={**dict(row),'receipt':json.loads(row['receipt'])}
        telegram=receipts.get('telegram');notion=receipts.get('notion')
    stamp=datetime.now(coach.KST).strftime('%Y-%m-%d %H:%M KST')
    notion_url=(notion or {}).get('receipt',{}).get('url','')
    preview=coach.STATE/'validation/weekly-preview.md'
    weekly=preview.read_text() if preview.exists() else '아직 주간 미리보기를 생성하지 않았습니다.'
    out=coach.STATE/'demo';out.mkdir(mode=0o700,exist_ok=True)
    body='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>차지욱 AI 실천 코치 · 동작 시연</title>
<style>
:root{font-family:Arial,"Noto Sans CJK KR",sans-serif;color:#142e40;background:#f3f5f6;font-size:16px}*{box-sizing:border-box}body{margin:0}main{max-width:1140px;margin:auto;padding:36px 30px 70px}header{display:flex;justify-content:space-between;gap:28px;align-items:center}h1{font-size:34px;letter-spacing:-1.3px;margin:8px 0 12px}h2{font-size:23px;letter-spacing:-.6px;margin:0 0 14px}h3{font-size:18px}p{line-height:1.7}.eyebrow{font-size:12px;font-weight:700;letter-spacing:1.5px;color:#506777}.muted{color:#60717f;font-size:14px}.badge{background:#e2eee7;color:#276348;border:1px solid #c0d9c9;border-radius:50px;padding:8px 14px;font-weight:bold;font-size:13px;white-space:nowrap}.status{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:26px 0}.status article{padding:17px 20px;background:white;border:1px solid #dce3e7;border-radius:12px}.status span{font-size:12px;color:#60717f}.status b{display:block;font-size:19px;margin:8px 0 4px}.status small{font-size:12px;color:#60717f}.panel{background:white;border:1px solid #dce3e7;border-radius:16px;padding:26px;margin-bottom:20px}.panelhead{display:flex;justify-content:space-between;align-items:start;gap:16px}.tag{font-size:12px;background:#edf1f4;border-radius:6px;padding:6px 9px;color:#5b6870}.buttons{display:flex;gap:9px;flex-wrap:wrap;margin:20px 0}button{border:1px solid #b9c8d1;background:white;border-radius:8px;padding:11px 16px;font-size:14px;font-weight:bold;color:#244153;cursor:pointer}button.primary{background:#16496a;color:white;border-color:#16496a}button:disabled{opacity:.45;cursor:default}button:focus-visible,a:focus-visible{outline:3px solid #da9838;outline-offset:3px}.grid{display:grid;grid-template-columns:1.7fr 1fr;gap:24px}table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:#60717f;font-size:12px;padding:12px 7px;border-bottom:2px solid #dce3e7}td{padding:16px 7px;border-bottom:1px solid #e5e9eb;font-variant-numeric:tabular-nums}.number{text-align:right}.totals{display:flex;justify-content:space-between;padding:14px 8px;font-size:15px;background:#f3f6f8}.result{border-radius:12px;background:#f3f6f8;padding:20px}.result .big{font-size:29px;font-weight:bold;margin:12px 0}.result p{font-size:13px;margin:7px 0}.result[data-state="broken"]{background:#fff1e7;color:#934c16}.result[data-state="fixed"]{background:#e9f4ec;color:#276348}.pill{font-size:12px;font-weight:bold;display:inline-block;background:#ffffff94;padding:5px 9px;border-radius:5px}.formula{padding:12px 14px;background:#152f41;color:#e2edf4;border-radius:8px;font-family:monospace;font-size:14px;margin-top:17px}.note{font-size:12px;color:#65727c;line-height:1.6;margin-bottom:0}.tiptitle{font-size:18px;font-weight:bold;color:#16496a}a{color:#155f91;text-decoration:none}a:hover{text-decoration:underline}details{margin:12px 0}summary{font-weight:bold;cursor:pointer;padding:10px 0}.prose{line-height:1.8;font-size:15px;padding:12px 2px}.prose h1{font-size:23px}.prose h2{margin-top:28px;font-size:20px}.prose pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f4f7;padding:18px;border-radius:10px;font-size:14px}.prose li{margin:8px 0}.prose a{overflow-wrap:anywhere}footer{font-size:12px;color:#60717f;line-height:1.8}.footlink{margin-top:18px;display:flex;gap:18px;flex-wrap:wrap}@media(max-width:750px){main{padding:22px 16px}h1{font-size:27px}header{display:block}.status{grid-template-columns:1fr}.grid{grid-template-columns:1fr}.panel{padding:20px}.panelhead{display:block}.badge{display:inline-block;margin-top:8px}th,td{padding-left:4px;padding-right:4px;font-size:12px}}
.stages{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:22px 0 18px}.stepcard{background:#f1f5f8;border-top:4px solid #5a7c96;border-radius:12px;padding:18px}.stepcard.broken{background:#fff2e8;border-color:#bc602a}.stepcard.fixed{background:#edf6ef;border-color:#378059}.stepamount{font-size:24px;font-weight:bold;margin:15px 0}.stepcard.broken .stepamount{color:#934c16}.stepcard.fixed .stepamount{color:#276348}.stepcard p{font-size:13px;line-height:1.8}.stepcard .formula{font-size:12px;padding:10px}.tablewrap{overflow-x:auto}summary:focus-visible{outline:3px solid #da9838;outline-offset:3px}@media(max-width:750px){.stages{grid-template-columns:1fr}}
</style><main><header><div><div class="eyebrow">JIOOK CHA · AGENTIC AI COACH</div><h1>오늘의 팁을 작업에 적용하면</h1><div class="muted">실제 전달·저장 결과와, 첫 팁을 적용하는 작은 시연입니다.</div></div><div class="badge">실제 기록 확인 · __STAMP__</div></header>
<section class="status" aria-label="실제 시스템 상태"><article><span>매일 오전 8시</span><b>일일 팁 전달</b><small>초기 시험 Telegram: __TELEGRAM__ · 시도 __ATTEMPTS__회</small></article><article><span>서버 + NOTION</span><b>같은 결과 누적 저장</b><small>현재 일일 기록 __COUNT__건 · Notion __NOTION__</small></article><article><span>매주 일요일 오후 6시</span><b>주간 요약 생성</b><small>미리보기: 기록 1건 · 예약 실행 결과 아님</small></article></section>
<p class="note">위 전달·저장 기록은 초기 동작 시험의 결과이며, 예약 시각에 실행됐다는 증거는 아닙니다.</p>
<section class="panel"><div class="panelhead"><div><div class="eyebrow">01 · 오늘의 행동을 직접 시험</div><h2 style="margin-top:10px">입력을 바꾸면 드러나는 수식 오류</h2><div class="muted">처음 숫자가 맞아 보이는 예산표에서, 재계산되는지 확인합니다.</div></div><span class="tag">가상 자료 · 실제 교수님 예산 아님</span></div>
<div class="stages" aria-label="가상 예산표의 세 단계 비교">
<article class="stepcard"><div class="eyebrow">시작 · 현재 숫자만 확인</div><h3>처음에는 맞아 보임</h3><div class="stepamount">총지출 350,000원</div><p>자료 10개 × 단가 20,000원</p><p>표의 자료 금액 <b>200,000원</b><br>표의 잔액 <b>650,000원</b><br>예상 총지출 <b>350,000원</b></p><div class="formula">자료 금액 = 200,000 (고정값)</div><p class="note">현재 값만으로는 고정값인지 수식인지 구별하지 못합니다.</p></article>
<article class="stepcard broken"><div class="eyebrow">1 · 단가 10,000원 인상</div><h3>100,000원 불일치 발견</h3><div class="stepamount">총지출 350,000원</div><p>자료 10개 × 단가 30,000원</p><p>표의 자료 금액 <b>200,000원</b><br>표의 잔액 <b>650,000원</b><br>예상 총지출 <b>450,000원</b></p><div class="formula">자료 금액 = 200,000 (고정값)</div><p class="note">단가는 올랐지만 금액·총지출·잔액이 그대로입니다.</p></article>
<article class="stepcard fixed"><div class="eyebrow">2 · 확인된 수식 수정</div><h3>재계산 결과 일치</h3><div class="stepamount">총지출 450,000원</div><p>자료 10개 × 단가 30,000원</p><p>표의 자료 금액 <b>300,000원</b><br>표의 잔액 <b>550,000원</b><br>예상 총지출 <b>450,000원</b></p><div class="formula">자료 금액 = 수량 × 단가</div><p class="note">단가 변경이 자료 금액·총지출·잔액에 반영됩니다.</p></article></div>
<details id="arithmetic"><summary>예상값과 표의 값을 나란히 확인하기</summary><p class="note">총예산 1,000,000원. 간식은 5개 × 30,000원 = 150,000원으로 모든 단계에서 같습니다. 단위: 원.</p><div class="tablewrap"><table><thead><tr><th>확인 항목</th><th class="number">시작</th><th class="number">단가 변경 후</th><th class="number">수식 수정 후</th></tr></thead><tbody><tr><td>자료 금액: 표 / 예상</td><td class="number">200,000 / 200,000</td><td class="number">200,000 / 300,000</td><td class="number">300,000 / 300,000</td></tr><tr><td>총지출: 표 / 예상</td><td class="number">350,000 / 350,000</td><td class="number">350,000 / 450,000</td><td class="number">450,000 / 450,000</td></tr><tr><td>잔액: 표 / 예상</td><td class="number">650,000 / 650,000</td><td class="number">650,000 / 550,000</td><td class="number">550,000 / 550,000</td></tr></tbody></table></div><p class="note">실제 시험에서는 같은 변경을 수정 전후 사본에 적용하고, 끝나면 단가를 원래 값으로 돌립니다.</p></details>
<p class="note">검증 기준: 단가 변경 전 예상값 계산 → 실제 재계산값 대조 → 확인된 수식 수정. 아래 시연은 오류를 의도적으로 넣은 설명용 예시이며, 실제 업무 품질이나 시간 절감 효과를 측정한 결과가 아닙니다.</p></section>
<section class="panel"><div class="eyebrow">02 · 실제 생성된 팁과 근거</div><h2 style="margin-top:10px" class="tiptitle">__TITLE__</h2><p class="muted">논문·공식 가이드·YouTube 후보를 읽은 후, 이 팁에는 논문 1건과 공식 가이드 1건을 근거로 사용했습니다.</p><details><summary>바로 쓸 지시문·확인 방법·원출처 펼치기</summary><div class="prose">__TIP__</div></details><div class="footlink"><a href="__NOTION_URL__" target="_blank" rel="noopener">Notion의 실제 기록 열기 ↗</a><a href="https://www.notion.so/" target="_blank" rel="noopener">누적 데이터베이스 열기 ↗</a></div></section>
<section class="panel"><div class="eyebrow">03 · 주간에 다시 보는 것</div><details><summary>주간 요약 미리보기 — 시험 기록 1건, 사용자 피드백 0건</summary><div class="prose">__WEEKLY__</div></details><p class="note">이 화면의 단계 비교와 펼치기는 시연용입니다. 교수님의 실제 사용·만족도·개선 효과로 저장하지 않습니다. 적용 피드백은 Telegram에서 /codex_coach와 함께 알려주세요.</p></section>
<footer>이 페이지는 __STAMP__의 실제 기록을 읽어 만든 시연 화면입니다. 실시간 대시보드는 아닙니다.<br>서버의 일일·주간 작업을 설명하는 시연이며, ChatGPT 자체 예약 상태는 이 화면에 포함하지 않았습니다.</footer></main>
</html>'''
    replacements={'__STAMP__':html.escape(stamp),'__TELEGRAM__':html.escape((telegram or {}).get('status','미확인')),
                  '__ATTEMPTS__':str((telegram or {}).get('attempts',0)),'__COUNT__':str(len(rows)),
                  '__NOTION__':html.escape((notion or {}).get('status','미확인')),
                  '__TITLE__':html.escape(d['title']),'__NOTION_URL__':html.escape(notion_url,quote=True),
                  '__TIP__':md(d['markdown']),'__WEEKLY__':md(weekly)}
    for key,value in replacements.items():body=body.replace(key,value)
    path=out/'index.html';path.write_text(body);path.chmod(0o600)
    print(json.dumps({'path':str(path),'digest_id':d['id'],'snapshot_at':stamp},ensure_ascii=False))


if __name__=='__main__':build()
