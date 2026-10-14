// SCRATCH (#289 evidence, reverted): an import-only file that registers no tests.
import test from 'node:test';
import { lintSql } from './lint-migrations.mjs';
void test;
void lintSql;
