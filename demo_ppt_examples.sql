-- 数据库原理 第 5 章 课件例题（精简内核版）
-- 用法：python sql_kernel.py 打开工作台，点「打开脚本」选这个文件，Ctrl+Enter 一次跑完。
-- 库里默认已有 Students / Courses / SC 三张表（空库首次打开会自动灌样例数据），
-- 所以这里不开新表就写 CREATE，避免和已有三张表撞名；新增的演示表叫 Dept。

-- ===== 1. 简单查询：投影、表达式、别名 =====
SELECT Sno, Sname, Sage FROM Students;
SELECT Sname, 2020 - Sage AS Birth, Sage * 2 AS Double FROM Students;
SELECT * FROM Students;
SELECT sno, sname FROM students;                  -- 标识符不区分大小写

-- ===== 2. 条件查询：比较、BETWEEN、IN、LIKE、IS NULL =====
SELECT Sname, Sage FROM Students WHERE Sage >= 20;
SELECT Sname FROM Students WHERE Sage BETWEEN 19 AND 20;
SELECT Sname, Sdept FROM Students WHERE Sdept IN ('CS', 'MA');
SELECT Sname FROM Students WHERE Sname LIKE '刘%';
SELECT Sname FROM Students WHERE Sname LIKE '_勇';
SELECT Sname FROM Students WHERE Sdept NOT IN ('IS');
SELECT Sno FROM SC WHERE Grade IS NULL;
SELECT Sname FROM Students WHERE Sage > 18 AND Sdept = 'IS';

-- ===== 3. 排序与限量 =====
SELECT Sno, Grade FROM SC WHERE Cno = 'C1' ORDER BY Grade DESC;
SELECT Sname, Sage FROM Students ORDER BY Sdept, Sage DESC;
SELECT Sname, Sage FROM Students ORDER BY 2 DESC;
SELECT * FROM Students LIMIT 3;
SELECT Sname FROM Students ORDER BY Sno LIMIT 2, 3;

-- ===== 4. 聚合与分组 =====
SELECT COUNT(*) FROM Students;
SELECT COUNT(Sno) FROM SC;
SELECT COUNT(DISTINCT Sno) FROM SC;
SELECT AVG(Grade), MAX(Grade), MIN(Grade) FROM SC WHERE Cno = 'C1';
SELECT Cno, COUNT(*) AS 人数, AVG(Grade) AS 均分 FROM SC GROUP BY Cno;
SELECT Cno, AVG(Grade) FROM SC GROUP BY Cno HAVING AVG(Grade) >= 80;
SELECT Sdept, COUNT(*) FROM Students GROUP BY Sdept HAVING COUNT(*) > 2;

-- ===== 5. 连接查询 =====
SELECT Students.Sname, SC.Cno, SC.Grade
  FROM Students, SC
 WHERE Students.Sno = SC.Sno;
SELECT a.Sname, b.Grade FROM Students a, SC b
 WHERE a.Sno = b.Sno AND b.Cno = 'C1';
SELECT s.Sname, c.Cname, sc.Grade
  FROM Students s, SC sc, Courses c
 WHERE s.Sno = sc.Sno AND sc.Cno = c.Cno AND c.Cname = 'DBMS';
SELECT s.Sname AS 姓名, s.Sage AS 年龄, t.Sname AS 同龄人
  FROM Students s, Students t
 WHERE s.Sage = t.Sage AND s.Sno < t.Sno;          -- 自连接，找同龄人
SELECT Students.Sname, SC.Cno FROM Students, SC          -- 没写连接条件就是笛卡尔积
 WHERE Students.Sno = SC.Sno AND Students.Sdept = 'MA';

-- ===== 6. 嵌套查询 =====
SELECT Sname FROM Students
 WHERE Sno IN (SELECT Sno FROM SC WHERE Cno = 'C1');
SELECT Sname, Sage FROM Students
 WHERE Sage > (SELECT AVG(Sage) FROM Students);    -- 不相关子查询，先算一次
SELECT Sname FROM Students
 WHERE EXISTS (SELECT * FROM SC WHERE SC.Sno = Students.Sno AND Cno = 'C1');
SELECT Sname FROM Students
 WHERE NOT EXISTS (SELECT * FROM SC WHERE SC.Sno = Students.Sno AND Cno = 'C2');
SELECT Sno, Cno, Grade FROM SC x
 WHERE Grade > (SELECT AVG(Grade) FROM SC y WHERE y.Sno = x.Sno);   -- 相关子查询
SELECT Sname FROM Students
 WHERE Sno IN (SELECT Sno FROM SC WHERE Cno IN
              (SELECT Cno FROM Courses WHERE Cname LIKE 'DB%'));
SELECT Cno FROM SC GROUP BY Cno
 HAVING AVG(Grade) > (SELECT AVG(Grade) FROM SC);
SELECT 1 + 1;

-- ===== 7. 数据更新 =====
INSERT INTO Students VALUES ('20140009', '赵新', '女', 20, 'CS');
SELECT COUNT(*) FROM Students;
INSERT INTO Students (Sno, Sname, Ssex, Sage) VALUES ('20140010', '钱多', '男', 19);
SELECT Sno, Sname, Sdept FROM Students WHERE Sno = '20140010';   -- Sdept 为空
UPDATE Students SET Sdept = 'IS' WHERE Sno = '20140010';
UPDATE SC SET Grade = Grade + 2 WHERE Cno = 'C1' AND Grade >= 80;
SELECT Sno, Grade FROM SC WHERE Cno = 'C1' ORDER BY Grade DESC;
DELETE FROM SC WHERE Sno = '20140009';
DELETE FROM Students WHERE Sno = '20140009';
SELECT COUNT(*) FROM Students;
UPDATE Students SET Sage = 23 WHERE Sno = '20140001';             -- 改非码列可以

-- ===== 8. 完整性与约束 =====
-- INSERT INTO SC VALUES ('20140010', 'C1', 120);      -- 放开会报：Grade 的 CHECK 不满足
-- INSERT INTO SC VALUES ('20999999', 'C1', 88);       -- 放开会报：外码在 Students 里找不到
INSERT INTO SC VALUES ('20140010', 'C1', 88);
-- INSERT INTO SC VALUES ('20140010', 'C1', 60);       -- 放开会报：违反唯一性约束 (Sno,Cno)
-- DELETE FROM Students WHERE Sno = '20140010';        -- 放开会报：这行还被 SC 引用着
DELETE FROM SC WHERE Sno = '20140010';
DELETE FROM Students WHERE Sno = '20140010';
SELECT COUNT(*) FROM Students;

-- ===== 9. 建表、建索引、删表 =====
CREATE TABLE Dept (
    Dno   char(4) PRIMARY KEY,
    Dname char(20) NOT NULL,
    Dhead char(20)
);
INSERT INTO Dept VALUES ('D1', '计算机系', '李勇');
INSERT INTO Dept VALUES ('D2', '信息系统系', '刘辰');
INSERT INTO Dept VALUES ('D3', '数学系', '王敏');
SELECT * FROM Dept;
SELECT Dname, Dhead FROM Dept WHERE Dno = 'D1';
UPDATE Dept SET Dhead = '欧阳娜娜' WHERE Dno = 'D1';
SELECT * FROM Dept ORDER BY Dno;
CREATE INDEX idx_dept_name ON Dept(Dname);
SELECT * FROM Dept WHERE Dname = '数学系';            -- 走索引
DROP INDEX idx_dept_name;
CREATE UNIQUE INDEX idx_std_sno ON Students(Sno);
SELECT Sname FROM Students WHERE Sno = '20140003';    -- 走唯一索引
DROP INDEX idx_std_sno;
DROP TABLE Dept;
SELECT COUNT(*) FROM Students;
SELECT DISTINCT Sdept FROM Students;
SELECT Sdept, COUNT(*) FROM Students GROUP BY Sdept ORDER BY 2 DESC;
