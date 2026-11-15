#pragma once

#include "sql/operator/physical_operator.h"

class Table;
class FieldMeta;

/**
 * @brief 更新物理算子
 * @ingroup PhysicalOperator
 */
class UpdatePhysicalOperator : public PhysicalOperator
{
public:
  UpdatePhysicalOperator(Table *table, const FieldMeta *field_meta, const Value &value)
      : table_(table), field_meta_(field_meta), value_(value)
  {}

  virtual ~UpdatePhysicalOperator() = default;

  PhysicalOperatorType type() const override { return PhysicalOperatorType::UPDATE; }
  OpType               get_op_type() const override { return OpType::UPDATE; }

  RC open(Trx *trx) override;
  RC next() override;
  RC close() override;

  Tuple *current_tuple() override { return nullptr; }

private:
  Table           *table_      = nullptr;
  const FieldMeta *field_meta_ = nullptr;
  Value            value_;
};
