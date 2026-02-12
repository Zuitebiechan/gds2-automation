# Day 7 准备工作完成

**日期**: 2026-02-12
**状态**: ✅ 配置完成，准备开始真实测试

---

## 完成的工作

### 1. 创建测试脚本

#### `test_real_recovery.py`
- 完整的Day 7测试脚本
- 包含3个测试场景：正常流程、超时恢复、弹窗恢复
- 自动加载 `.env` 环境变量
- LLM调用次数监控
- 决策日志分析功能

#### `verify_config.py`
- 配置验证脚本
- 检查5个方面：环境变量、配置加载、ZhipuAI SDK、日志目录、GDS2数据目录
- 自动加载 `.env` 环境变量
- 提供详细的错误诊断

### 2. 配置 `.env` 文件

已更新以下配置：

```bash
# AI Exception Recovery (已激活)
ENABLE_AI_RECOVERY=true
AI_RECOVERY_PROVIDER=zhipuai
AI_RECOVERY_MODEL=glm-4-plus
ZHIPUAI_API_KEY=e85242ac28f54929b44e58daf49bebb9.Yn7CtzeLGzBAR9s6

# Recovery behavior
AI_RECOVERY_CONFIDENCE=0.7
AI_RECOVERY_MAX_RETRIES=2
AI_RECOVERY_MAX_CALLS=3

# Logging
AI_RECOVERY_LOG_DIR=logs/ai_recovery
AI_RECOVERY_LOG_DECISIONS=true
```

### 3. 验证配置

运行 `python verify_config.py` 的结果：

```
✅ 所有检查通过! 可以开始Day 7测试

验证结果:
  ✅ 通过 - 环境变量
  ✅ 通过 - 配置加载
  ✅ 通过 - ZhipuAI SDK
  ✅ 通过 - 日志目录
  ✅ 通过 - GDS2数据目录
```

---

## Day 7 测试步骤

### 前置条件

- [x] AI恢复系统配置完成
- [x] ZhipuAI API Key已配置
- [x] 测试脚本已创建
- [ ] GDS2软件已启动
- [ ] VCI设备已连接

### 测试场景

#### 场景1：正常流程（基准测试）

**目标**: 验证AI恢复不影响正常流程

**步骤**:
```bash
# 1. 启动GDS2
# 2. 确保VCI设备已连接
# 3. 运行测试
python test_real_recovery.py
```

**预期**:
- ✅ Workflow正常运行
- ✅ LLM调用次数 = 0（无异常）
- ✅ 无恢复操作

---

#### 场景2：超时恢复

**目标**: 测试AI处理超时异常

**步骤**:
1. 启动workflow
2. **在等待Enter按钮时拔掉VCI设备**
3. 观察日志输出
4. 检查 `logs/ai_recovery/decisions.jsonl`

**预期**:
- ✅ AI检测到超时
- ✅ AI决策：WAIT_LONGER 或 ABORT
- ✅ 日志记录完整
- ✅ LLM调用次数 = 1

**验证**:
```bash
# 查看决策日志
tail -5 logs/ai_recovery/decisions.jsonl | python -m json.tool
```

---

#### 场景3：弹窗恢复

**目标**: 测试AI处理意外对话框

**步骤**:
1. 启动workflow
2. **触发GDS2错误对话框**（例如：断网、VCI断开）
3. 观察AI是否自动点击OK
4. 检查决策日志

**预期**:
- ✅ AI检测到弹窗
- ✅ AI决策：CLICK_BUTTON("OK")
- ✅ 弹窗自动关闭
- ✅ Workflow继续运行

---

### 实时监控

```bash
# 终端1：运行workflow
python test_real_recovery.py

# 终端2：实时查看AI决策日志
tail -f logs/ai_recovery/decisions.jsonl

# 终端3：监控LLM调用
watch -n 1 'grep "LLM调用" logs/ai_recovery/decisions.jsonl | wc -l'
```

---

## 成功标准

### 必须达标 (Must Have)

- [x] 配置加载成功
- [ ] 正常流程工作（无回归）
- [ ] AI检测到至少1个异常
- [ ] AI成功恢复至少1次
- [ ] 决策日志正确生成
- [ ] LLM调用在限制内（≤3次/session）

### 应该达标 (Should Have)

- [ ] 恢复成功率 ≥80%
- [ ] 单次恢复成本 <$0.02
- [ ] 恢复时间 <10秒
- [ ] 无无限递归
- [ ] 无异常崩溃

### 可选达标 (Nice to Have)

- [ ] 恢复成功率 ≥90%
- [ ] 单次恢复成本 <$0.01
- [ ] 恢复时间 <5秒

---

## 成本估算

### ZhipuAI GLM-4-Plus 定价

- **输入**: ¥50/MTok (~$7/MTok)
- **输出**: ¥50/MTok (~$7/MTok)

### 单次恢复成本

- 输入: ~1500 tokens × $7/MTok = **$0.0105**
- 输出: ~100 tokens × $7/MTok = **$0.0007**
- **总计: ~$0.0112/次**

### 测试预算

- 预计测试10-20次异常
- 总成本: 20 × $0.0112 = **$0.22**
- 月度成本（生产）: ~$3.36/月（100会话/天，10%失败率）

---

## 故障排除

### 问题1：AI recovery未启用

**症状**: `workflow.recovery is None`

**解决**:
```bash
python verify_config.py
```

### 问题2：ZhipuAI API调用失败

**症状**: `zhipuai.APIError`

**解决**:
```python
# 测试API连接
from zhipuai import ZhipuAI

client = ZhipuAI(api_key="e85242ac28f54929b44e58daf49bebb9.Yn7CtzeLGzBAR9s6")
response = client.chat.completions.create(
    model="glm-4-plus",
    messages=[{"role": "user", "content": "测试"}],
)
print(response.choices[0].message.content)
```

### 问题3：决策日志不生成

**原因**:
1. `AI_RECOVERY_LOG_DECISIONS=false`
2. 没有触发任何异常

**解决**:
```bash
# 检查配置
echo $AI_RECOVERY_LOG_DECISIONS

# 手动触发异常（拔VCI设备）
```

---

## 下一步

1. **启动GDS2** + VCI设备连接
2. **运行测试**: `python test_real_recovery.py`
3. **触发异常**: 拔VCI设备或触发错误对话框
4. **检查日志**: `logs/ai_recovery/decisions.jsonl`
5. **分析成本**: 查看LLM调用次数
6. **记录结果**: 更新Day 7测试报告

---

## 文件清单

### 新增文件

- `test_real_recovery.py` - Day 7测试脚本
- `verify_config.py` - 配置验证脚本
- `docs/DAY7_SETUP_COMPLETE.md` - 本文档

### 修改文件

- `.env` - 更新AI恢复配置（启用ZhipuAI）

---

**准备工作完成! 可以开始Day 7真实测试**

参考文档: `docs/AI_RECOVERY_DAY7_GUIDE.md`
