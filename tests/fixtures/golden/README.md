# Golden fixtures (WP0)

Raw chain data captured by `tools/capture_golden.py` (plain httpx JSON-RPC, read-only) for the verified
vectors of DESIGN.md section 10.1. Every value is pinned to a block hash. Storage values are the exact hex
SCALE bytes returned by `state_queryStorageAt` (`null` = absent key; absent ValueQuery keys take the
runtime default). Runtime-API results are the exact hex returned by `state_call`. `expected` holds the
brief's reference numbers; `checks` holds convenience decodes made by the capture tool (not authoritative).

- Endpoint: `https://bittensor-finney.api.onfinality.io/public`
- Captured (UTC): 2026-10-09T03:28:05Z
- JSON-RPC calls: 765 (retries: 0), rate <= 2.5 req/s
- Regenerate: `.venv/Scripts/python.exe tools/capture_golden.py` (or `--only NAME`; `--readme-only` rewrites
  this file offline). manifest.json holds each file's sha256 over LF-normalized bytes.

## Provenance

| File | Block | Block hash | Spec | Tx | Purpose |
|---|---|---|---|---|---|
| emission_parity/b8766000.json | 8,765,999 | `0x72a51d71c94bf9ba9160ed0b650b79fe9d8f6e755e1dc5d31407cbbe3b47006b` | 441 | 1 | Emission parity pair: state at 8765999 (inputs, EMA_(n-1)) and 8766000 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,766,000 | `0x37ba886e5e76882b6d48b270d852b51f24b74bd2e68318c343ae0f3cbbc1e8b1` | 441 | 1 |  |
| emission_parity/b8789720.json | 8,789,719 | `0x79d051c58cfa29a575622ef9d96a4b41f6d834ce8e97e249c4686e61477c398f` | 443 | 1 | Emission parity pair: state at 8789719 (inputs, EMA_(n-1)) and 8789720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,789,720 | `0xb87d8e04bed8be3c949cc4fc68db9ce97941bc0177e890b06bd1c12c44ee836f` | 443 | 1 |  |
| emission_parity/b8813880.json | 8,813,879 | `0x77932b32f5ee40e8ee55d26c2affa0a2201c09c32bdc01d78057a565805421eb` | 443 | 1 | Emission parity pair: state at 8813879 (inputs, EMA_(n-1)) and 8813880 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,813,880 | `0x6ad19e14fdbec33f3a135dc264b4cf3dad09930112865da53ec42d595d145d81` | 443 | 1 |  |
| emission_parity/b8837720.json | 8,837,719 | `0x2af53c2cfa8cee87491ceda04c5ceda8c358d36b94fcb83c2b9b8ccaaf795d4d` | 445 | 1 | Emission parity pair: state at 8837719 (inputs, EMA_(n-1)) and 8837720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,837,720 | `0x4e7fb596e1af1b47d1d221e2952f695e81688b91e76d88cb25e0ebf6918e7fbb` | 445 | 1 |  |
| emission_parity/b8861760.json | 8,861,759 | `0x4ffec9abb5721165f2d76fa7b531fa43481b086aa207d76f7f06786bdc929a66` | 447 | 1 | Emission parity pair: state at 8861759 (inputs, EMA_(n-1)) and 8861760 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,861,760 | `0xe9ecea31fd55e422aed27cf0ca3d924e150b6144b6f191098a3aa69892e560fb` | 447 | 1 |  |
| emission_parity/b8885720.json | 8,885,719 | `0xa742aea191340420f40985d3e35f58d2527a472c76b7e70fb87d3344a00da6a4` | 447 | 1 | Emission parity pair: state at 8885719 (inputs, EMA_(n-1)) and 8885720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,885,720 | `0xa01d3908c60d76fe34884c7050c5710abc70663cf649a287892d0fa623f76719` | 447 | 1 |  |
| emission_parity/b8910000.json | 8,909,999 | `0x08e9e7e7e215a78f8eceb16f35ee3cdc553b0c8992deb521ed6dd59f2727e538` | 448 | 1 | Emission parity pair: state at 8909999 (inputs, EMA_(n-1)) and 8910000 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,910,000 | `0x6222e636a70d0107fbde34f963af3b63995c4fdf4e493827d8da4cb67cd23051` | 448 | 1 |  |
| emission_parity/b8933720.json | 8,933,719 | `0xd8d459b6f6ea578588798d3109bbf2a464ad6e6ed4c6e3e1598ea4a7155118fd` | 448 | 1 | Emission parity pair: state at 8933719 (inputs, EMA_(n-1)) and 8933720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,933,720 | `0x35e69ce2ea3f0c8ca27ce2570984188ecd65256218593961eb62067ab2c1f2ce` | 448 | 1 |  |
| emission_parity/b8957880.json | 8,957,879 | `0x8d27798803d6211cb08918ae7f5999547b0e6d55a76222bab3aee4549c43ca92` | 452 | 1 | Emission parity pair: state at 8957879 (inputs, EMA_(n-1)) and 8957880 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,957,880 | `0x808b495bd103ac2dc93e477dcd81b755238a57da505cf1d3c9c550f89448b7d0` | 452 | 1 |  |
| emission_parity/b8981720.json | 8,981,719 | `0x02aab14f07351eb59d28f69dd73c27463de6c0ed7c4307ba94d47fee4a70ff03` | 452 | 1 | Emission parity pair: state at 8981719 (inputs, EMA_(n-1)) and 8981720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 8,981,720 | `0x43d3e7166aae2e83bd763c4aae04e2c223ba763820f85b5198167cd5421ce65e` | 452 | 1 |  |
| emission_parity/b9005760.json | 9,005,759 | `0x2e78d601e24f22720b053b19433b283ee6af8326fb71070f8015ea729857992d` | 454 | 1 | Emission parity pair: state at 9005759 (inputs, EMA_(n-1)) and 9005760 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,005,760 | `0xf3735ae32fa3d166ed535f07036d9ef120f4923a728496f4508461ccf18f9d6d` | 454 | 1 |  |
| emission_parity/b9029720.json | 9,029,719 | `0xd120c5f1971ad419a17ac73eeb3844a0184fe4752bc32bae52619e5ad12ca216` | 455 | 1 | Emission parity pair: state at 9029719 (inputs, EMA_(n-1)) and 9029720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,029,720 | `0xcbcb0f0562ae2c98976e70a0e35509dd84c6a6e3cb61738aa00b6fca86eb08d3` | 455 | 1 |  |
| emission_parity/b9054000.json | 9,053,999 | `0x99f4d25106c2597ea663c24a096621d9d310bbb0f898d2fb76c1e9b9db49001a` | 455 | 1 | Emission parity pair: state at 9053999 (inputs, EMA_(n-1)) and 9054000 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,054,000 | `0x213af6a5d5efaf810f4ea22b92b1efa9992e53b3f9bfef899aeb3550f6630659` | 455 | 1 |  |
| emission_parity/b9077720.json | 9,077,719 | `0x2458b97bd4c43d7f530662671d28a6558c98b57be6b87863df8917fbe7f21974` | 459 | 1 | Emission parity pair: state at 9077719 (inputs, EMA_(n-1)) and 9077720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,077,720 | `0x90bed0baae9125381446f8cfd4c52fa272fd37e9e8157a8f03de5426f2f3675f` | 459 | 1 |  |
| emission_parity/b9101880.json | 9,101,879 | `0x607df53511474e9be5d50688efaee5e4bc73f2cb41bd7575c094a1ddb9670553` | 467 | 1 | Emission parity pair: state at 9101879 (inputs, EMA_(n-1)) and 9101880 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,101,880 | `0x45019965ade87be7fbe3a10bde658fbcf22fe880db8d283d6ea7da7630279cf9` | 467 | 1 |  |
| emission_parity/b9125720.json | 9,125,719 | `0x1ec129465217078a4af61a86bb6b0f4ad5f30662837f16571296e8bdcb761af2` | 469 | 1 | Emission parity pair: state at 9125719 (inputs, EMA_(n-1)) and 9125720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,125,720 | `0x69b689654563f63044420af619c9f48b2018bf9c57b1adf9529a4fbb08e8b630` | 469 | 1 |  |
| emission_parity/b9149760.json | 9,149,759 | `0x477ac10366a22e9ac273ff4c2de0af6ddc5dd89328cfe9d2c8803f1525423dd2` | 470 | 1 | Emission parity pair: state at 9149759 (inputs, EMA_(n-1)) and 9149760 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,149,760 | `0x9b0bde56e31fdf8cb34213d5abf04f3e313c7348ed1e2951a25df5fde452cf61` | 470 | 1 |  |
| emission_parity/b9173720.json | 9,173,719 | `0x873b6fdd1eb63cfc83c8106ee931ed194abd295b1b0bc542946ef4fc8baaf911` | 470 | 1 | Emission parity pair: state at 9173719 (inputs, EMA_(n-1)) and 9173720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,173,720 | `0x1eb95695081dfbd8422490a1c81f75f1e3e893f658da2cbf94396e504e50d722` | 470 | 1 |  |
| emission_parity/b9198000.json | 9,197,999 | `0xd4570573aae63874f2dced76fac949c05dcb4aa6f27156ed7d364bb4c538dbad` | 472 | 1 | Emission parity pair: state at 9197999 (inputs, EMA_(n-1)) and 9198000 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,198,000 | `0x96c2104ca9aeb88f5133c3afd90f3683980784dc47c7e8341d68b3f4f0462282` | 472 | 1 |  |
| emission_parity/b9221720.json | 9,221,719 | `0x08969d01d3983989a890fb5f7e18ddebabe13031cbb507251780b41107afe5e9` | 473 | 1 | Emission parity pair: state at 9221719 (inputs, EMA_(n-1)) and 9221720 (observed SubnetTaoInEmission + SubnetExcessTao, reservoirs) for every netuid 0..144 |
|  | 9,221,720 | `0xc58b78bf0b0ba8a2b8fe4bbccb41857caaefaabee74c48cb426cafc417178adc` | 473 | 1 |  |
| erab_7000020.json | 7,000,020 | `0x8a362ac93297572fa4abfbf8f89dade28054ec9d835165f360111c44fd286805` | 348 | 1 | Era-B (swap v3) pools of SN1/SN19/SN64 at 7,000,020: SubnetTAO/AlphaIn plus Swap.AlphaSqrtPrice and Swap.CurrentLiquidity (virtual reserves L*sqrtP, L/sqrtP) with sim_swap buys of 1/10/100 TAO and sells of about 1/10/100 TAO of alpha (32-byte SimSwapResult at this spec) |
| escrow_9240388.json | 9,240,388 | `0xa57ba6d8ca74f815cb2f4b50be731fbc7e612128b4d8a5d98de30e11e9408524` | 475 | 1 | Raw validator-basket and escrow-coldkey stake results at 9,240,388 (SCALE structs decoded with scalecodec + runtime-API metadata; DESIGN.md section 13 Q7) |
| globals_9240878.json | 9,240,878 | `0xa8efb421200725c10ac5c72182d39833c14b6004b7a199708b219e9edea422f7` | 475 | 1 | Globals and prune-ladder inputs at 9,240,878 with the runtime registration cost |
| metadata_spec475_9240388.json | 9,240,388 | `0xa57ba6d8ca74f815cb2f4b50be731fbc7e612128b4d8a5d98de30e11e9408524` | 475 | 1 | raw runtime metadata (state_getMetadata) |
| metadata_storage_spec348.json | 7,000,020 | `0x8a362ac93297572fa4abfbf8f89dade28054ec9d835165f360111c44fd286805` | 348 | 1 | storage layout (hashers, key/value types, defaults) decoded from the metadata with scalecodec |
| metadata_storage_spec441.json | 8,765,720 | `0x4ded6e469234aa4d30113c7f13923ad1045c6ef5ea6ad5d7ea3d4b136812bc77` | 441 | 1 | storage layout (hashers, key/value types, defaults) decoded from the metadata with scalecodec |
| metadata_storage_spec475.json | 9,240,388 | `0xa57ba6d8ca74f815cb2f4b50be731fbc7e612128b4d8a5d98de30e11e9408524` | 475 | 1 | storage layout (hashers, key/value types, defaults) decoded from the metadata with scalecodec |
| sn1_quote_9240388.json | 9,240,388 | `0xa57ba6d8ca74f815cb2f4b50be731fbc7e612128b4d8a5d98de30e11e9408524` | 475 | 1 | SN1 pool and the 1-TAO sim_swap quote vector (brief 2.8) |
| sn51_emission_9240382.json | 9,240,381 | `0x563477199d8e8706d8fd42a7509378f553d92fbd856a40fdeeb2c0f1c5612cdd` | 475 | 1 | Full emission state at 9,240,381 (inputs: EMA_{n-1}) and 9,240,382 (observed per-block emission) for the SN51 injection-split vector |
|  | 9,240,382 | `0x36fe0397ffe2bd34a32fa6bdf26f6f21b50ecd595171c8d85ad26046290dfa89` | 475 | 1 |  |
| sn70_index_9240222_9240582.json | 9,240,222 | `0xe322b6f098fd927f1e026d2b4b6c9aaae1fe3b170a25f3fdd8aab8d8690999c5` | 475 | 1 | SN70 share-price index: flat 9,240,222 -> 9,240,581, then +0.0279% at 9,240,582 (= LastEpochBlock); index I = TotalHotkeyAlpha / shares (V2 SafeFloat when V1 is absent) |
|  | 9,240,581 | `0x081f2fbf7d5624ab01929d1b38a35fcc082c275f622ad78f14df206eb327ab11` | 475 | 1 |  |
|  | 9,240,582 | `0xbb2e6f444bc9491f9c147143e8748797572a672a69d1afeb861925b126d263fc` | 475 | 1 |  |
|  | 9,024,582 | `0x7955bd3d5761d8e336e1b8d6d40199580764e309835707c513b48a3c1ff1cf2b` | 455 | 1 |  |
| sn92_9240388.json | 9,240,388 | `0xa57ba6d8ca74f815cb2f4b50be731fbc7e612128b4d8a5d98de30e11e9408524` | 475 | 1 | SN92 pool, dividend panel, owner position and prune ladder at 9,240,388 (AMM 10/100-TAO buy vectors, live prune target, recovery-ratio inputs, closed-form yield inputs) |
| yield_inputs_9240388.json | 9,240,388 | `0xa57ba6d8ca74f815cb2f4b50be731fbc7e612128b4d8a5d98de30e11e9408524` | 475 | 1 | Closed-form yield inputs for SN70 / SN92 / SN64 (RootProp, alpha_out emission, owner cut, A_earn = sum of TotalHotkeyAlpha over AlphaDividendsPerSubnet recipients, takes) |

## Notes

- `sn1_quote_9240388.json`: spot == 6,562,800 at this block: True
- `sn1_quote_9240388.json`: DESIGN.md 10.1 lists alpha_out 152,285,961,807 rao; no block in [9,238,000, 9,242,700] reproduces it. At the brief's spot blocks sim_swap returns about 152,290,64x,xxx rao (152.29 alpha, as the brief says); the checks below hold the value at this block. ADR requested to correct the design figure.
- `sn70_index_9240222_9240582.json`: hotkey 56a9 = 0x56a9aee6291bd03ab6d36d4d13e2bebae7cd403518066c72fba1b417d6ddd748; the 4th snapshot (30 d = 216,000 blocks earlier) holds only its index inputs
- `yield_inputs_9240388.json`: The brief's yield table was measured around blocks 9,240,3xx; values here are at 9,240,388 and should agree to the brief's rounding.

## Capture-time checks (decoded by the tool; compare with each file's `expected`)

- `erab_7000020.json`: `{"SN1":{"current_alpha_price_rao":9322030,"alpha_sqrt_price_raw":1781045380923513843,"sells":{"1":{"tao_amount":999453341,"alpha_amount":107218747871,"tao_fee":0,"alpha_fee":54016956},"10":{"tao_amount":9990655141,"alpha_amount":1072187478712,"tao_fee":0,"alpha_fee":540169564},"100":{"tao_amount":99520372812,"alpha_amount":10721874787124,"tao_fee":0,"alpha_fee":5401695642}}},"SN19":{"current_alpha...`
- `escrow_9240388.json`: `{"baskets_bytes":203031,"escrow_stake_info_bytes":573461}`
- `globals_9240878.json`: `{"registration_cost_rao":962887016780}`
- `sn1_quote_9240388.json`: `{"sim_buy_1tao":{"tao_amount":999496453,"alpha_amount":152290647774,"tao_fee":503547,"alpha_fee":0,"tao_slippage":0,"alpha_slippage":83337619},"current_alpha_price_rao":6562800}`
- `sn92_9240388.json`: `{"sim_buy_10tao":{"tao_amount":9994964523,"alpha_amount":7289425629146,"tao_fee":5035477,"alpha_fee":0,"tao_slippage":0,"alpha_slippage":127811335036},"sim_buy_100tao":{"tao_amount":99949645228,"alpha_amount":63351665005924,"tao_fee":50354772,"alpha_fee":0,"tao_slippage":0,"alpha_slippage":10820704635900},"current_alpha_price_rao":1348210,"subnet_to_prune_raw":"0x015c00","n_dividend_recipients":52...`
- `yield_inputs_9240388.json`: `{"n_dividend_recipients":{"70":49,"92":52,"64":57}}`

## Layout

Each fixture: `{fixture, purpose, endpoint, captured_utc, snapshots: [{block, block_hash, spec_version,
transaction_version, storage: [{item, args, hashers, key, value}], storage_by_netuid?: {item: {hashers,
values[netuid]}}, runtime_api: [{method, args, args_hex, result, error?}], keys_paged?: [{label, prefix,
keys}]}], expected, checks, notes}`. `hashers` lists one hasher per key part (empty for plain values);
`args` are the decoded key parts (netuids, 0x account ids, enum labels). `storage_by_netuid` holds bulk
per-netuid reads for netuids 0..144 densely (index = netuid); their keys are prefix ++ Identity(u16) for
SubtensorModule items and prefix ++ Twox64Concat(u16) for Swap items. `metadata_storage_spec*.json` are
storage layouts decoded from runtime metadata with scalecodec (hashers, key/value types, defaults).
