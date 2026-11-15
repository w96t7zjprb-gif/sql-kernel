#pragma once

#include "sql/operator/logical_operator.h"

class Table;
class FieldMeta;

/**
 * @brief 更新逻辑算子
 * @ingroup LogicalOperator
 */
class UpdateLogicalOperator : public LogicalOperator
{
public:
  UpdateLogicalOperator(Table *table, const FieldMeta *field_meta, const Value &value)
      : table_(table), field_meta_(field_meta), value_(value)
  {}

  virtual ~UpdateLogicalOperator() = default;

  LogicalOperatorType type() const override { return LogicalOperatorType::UPDATE; }
  OpType              get_op_type() const override { return OpType::LOGICALUPDATE; }

  Table           *table() const { return table_; }
  const FieldMeta *field_meta() const { return field_meta_; }
  const Value     &value() const { return value_; }

private:
  Table           *table_      = nullptr;
  const FieldMeta *field_meta_ = nullptr;
  Value            value_;
};
