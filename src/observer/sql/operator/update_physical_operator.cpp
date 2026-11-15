#include "sql/operator/update_physical_operator.h"

#include "common/log/log.h"
#include "storage/table/table.h"
#include "storage/trx/trx.h"

using namespace std;

RC UpdatePhysicalOperator::open(Trx *trx)
{
  if (children_.empty()) {
    return RC::SUCCESS;
  }

  RC rc = children_[0]->open(trx);
  if (OB_FAIL(rc)) {
    return rc;
  }

  vector<pair<Record, Record>> updates;
  while (OB_SUCC(rc = children_[0]->next())) {
    Tuple *tuple = children_[0]->current_tuple();
    if (tuple == nullptr) {
      rc = RC::INTERNAL;
      break;
    }

    RowTuple *row_tuple = static_cast<RowTuple *>(tuple);
    Record    old_record = row_tuple->record();

    const TableMeta &table_meta = table_->table_meta();
    const int        normal_field_num = table_meta.field_num() - table_meta.sys_field_num();
    vector<Value>    values;
    values.reserve(normal_field_num);
    for (int i = 0; i < normal_field_num; i++) {
      Value value;
      rc = tuple->cell_at(i, value);
      if (OB_FAIL(rc)) {
        break;
      }
      if (0 == strcmp(table_meta.field(i + table_meta.sys_field_num())->name(), field_meta_->name())) {
        value = value_;
      }
      values.push_back(value);
    }
    if (OB_FAIL(rc)) {
      break;
    }

    Record new_record;
    rc = table_->make_record(normal_field_num, values.data(), new_record);
    if (OB_FAIL(rc)) {
      break;
    }
    updates.emplace_back(std::move(old_record), std::move(new_record));
  }

  children_[0]->close();
  if (rc != RC::RECORD_EOF) {
    return rc;
  }

  for (auto &update : updates) {
    rc = table_->update_record_with_trx(update.first, update.second, trx);
    if (OB_FAIL(rc)) {
      LOG_WARN("failed to update record. table=%s, rc=%s", table_->name(), strrc(rc));
      return rc;
    }
  }
  return RC::SUCCESS;
}

RC UpdatePhysicalOperator::next() { return RC::RECORD_EOF; }

RC UpdatePhysicalOperator::close() { return RC::SUCCESS; }
