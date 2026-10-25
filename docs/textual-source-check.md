# Textual開發依賴來源核對

2026-10-24；使用者已明確授權僅本工作樹專用venv的必要開發依賴。系統Python、VM與主機服務未安裝／修改。沒有執行遠端安裝腳本。

核對官方[Textualize/textual v8.2.8 metadata](https://github.com/Textualize/textual/blob/v8.2.8/pyproject.toml)的name=textual、version=8.2.8與homepage／repository，對應[PyPI textual8.2.8](https://pypi.org/project/textual/8.2.8/)。[官方安全公告頁](https://github.com/Textualize/textual/security/advisories)當次沒有已發布公告，且未設SECURITY.md；不代表沒有未知問題或零風險。

PyPI此版wheel沒有provenance／Trusted Publishing證據，故沒有宣稱可驗證建置來源。從官方https://pypi.org/simple解析，只接受files.pythonhosted.org的wheel；textual wheel SHA256為267375fd402dc8d981457212efa71f0e3365fd17bba144ba9bb3ed7563cb374a，與公開PyPI發行檔資訊一致。解析全部9個套件的指定版本PyPI JSON，當次vulnerabilities欄位皆空，逐一核對artifact hash；這是公告／來源一致性檢查，不是源碼完整安全稽核。

安裝使用venv內pip、isolated模式、官方index、only-binary、require-hashes及no-deps（完整依賴集合已解析鎖定），無不明鏡像、sdist build hook或全域安裝。完整鎖定見requirements-tui.lock；原始安裝計畫、來源核對與報告保存在被忽略的.venv內，不含應提交主機資料。

已裝：textual8.2.8、rich15.0.0、markdown-it-py4.2.0、mdit-py-plugins0.6.1、linkify-it-py2.2.0、mdurl0.1.2、platformdirs4.13.5、Pygments2.21.0、typing_extensions4.16.0。venv pip25.0.1來自Python內建ensurepip。pip check通過；.venv/Scripts/python.exe經git check-ignore確認忽略。Windows venv不能直接複製作Linux安裝，Linux需另依核准流程重建／驗證。

安全查證的時間與來源範圍有限，未做獨立漏洞掃描或終端逃逸全面分析。本工具對主機值以純文字呈現、無動態markup／遠端內容執行，也不暴露Textual web服務；仍需持續留意上游公告。
