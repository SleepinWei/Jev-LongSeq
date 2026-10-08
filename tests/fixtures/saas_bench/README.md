`business_031_verify.py` is the unmodified upstream SaaS-Bench oracle from
commit `48c22deb18b98c0ed78f81e7f3be82bc162de2c8`:
https://github.com/UniPat-AI/SaaS-Bench/blob/48c22deb18b98c0ed78f81e7f3be82bc162de2c8/tasks/uni-m/Business/business_031/verify.py

It is a test fixture only. Production reads the trusted Mac mini checkout and
writes the versioned compatibility copy into each run's output directory.

`business_031_v11_verify.py` is the unmodified v1.1 official oracle from
`aaa042140b585f80ad531bc8c9af296581426873`:
https://github.com/UniPat-AI/SaaS-Bench/blob/aaa042140b585f80ad531bc8c9af296581426873/tasks/uni-m/Business/business_031/verify.py

Production executes this audited source verbatim, without the legacy patch.
