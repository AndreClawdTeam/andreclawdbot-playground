# PRD Técnico — R2: Conditional Approval Processing

> **Versão:** v4 (final)  
> **Autor:** Johnny Juvenil 🫡  
> **Status:** Aprovado — pronto para implementação  
> **Objetivo:** Guia completo de implementação do workflow R2 no Zora Pantheon (HouseNumbers)

---

## Sumário

1. [Visão Geral](#1-visão-geral)
2. [Arquitetura: Opção B (Event-Driven)](#2-arquitetura-opção-b-event-driven)
3. [Fluxo End-to-End](#3-fluxo-end-to-end)
4. [agent-service — R2 Handler](#4-agent-service--r2-handler)
5. [mcp-gateway-service — Novas Tools](#5-mcp-gateway-service--novas-tools)
6. [communication-service — collection condition_outreach_drafts](#6-communication-service--collection-condition_outreach_drafts)
7. [agent-service — R7 Handler (Document Task Auto-Match)](#7-agent-service--r7-handler-document-task-auto-match)
8. [Confidence Gates](#8-confidence-gates)
9. [Plano de Testes](#9-plano-de-testes)
10. [Mapa Completo de Arquivos](#10-mapa-completo-de-arquivos)
11. [Variáveis de Ambiente](#11-variáveis-de-ambiente)
12. [Checklist de Implementação](#12-checklist-de-implementação)

---

## 1. Visão Geral

### Problema

Quando o lender envia um email de **conditional approval**, o MLP recebe um PDF com lista de condições (requisitos para documentação/fundos). Hoje, o MLP precisa manualmente:

1. Abrir o PDF e ler as condições
2. Criar tasks no Zora para cada condição
3. Verificar quais docs já estão upados
4. Identificar quem contatar para coletar o que falta
5. Escrever emails de outreach

Isso leva 30–60 minutos por loan, com alto risco de erro humano.

### Solução

O workflow R2 automatiza esse processo end-to-end:

- **Trigger:** Upload do PDF de conditional approval → Ocrolus classifica como `"Conditional Approval"` → `DOCUMENT_UPSERT` event
- **Tier 1:** String equality `classification?.toLowerCase() === "conditional approval"` — O(1), sem LLM
- **Tier 2:** Mastra Agent lê o PDF via LLM → extrai condições → cria tasks → R7 faz doc matching → drafta emails de outreach
- **Output:** MLP recebe tudo pronto no Agent Inbox — apenas revisa e aprova

### Workflows envolvidos

| ID  | Nome                         | Trigger         | Status        |
|-----|------------------------------|-----------------|---------------|
| R2  | Conditional Approval Processing | `DOCUMENT_UPSERT` | **Implementar** |
| R7  | Document Task Auto-Match     | `TASK_CREATED`  | **Implementar** |
| R3  | Document Receipt Matching    | `DOCUMENT_UPSERT` | Stub existente |
| R6  | Auto-Advance Tasks on Evidence | `DOCUMENT_UPSERT` | Stub existente |

---

## 2. Arquitetura: Opção B (Event-Driven)

### Decisão

**R2 cria tasks → TASK_CREATED dispara R7 → R7 faz doc matching + email drafting**

Vs. Opção A (monolítica), a Opção B:
- ✅ Segue a arquitetura existente (event-driven)
- ✅ R7 é reutilizável para outros casos (task criada manualmente, por exemplo)
- ✅ Handlers menores e mais testáveis
- ✅ Falha isolada: se R7 travar, as tasks já foram criadas

### Sequência de eventos

```
DOCUMENT_UPSERT (classification: "conditional approval")
  └── R2 executa
       └── CREATE_TASK × N → persistence
            └── task-service publica TASK_CREATED × N
                 └── R7 executa para cada task
                      └── DRAFT_EMAIL × M + UPDATE_TASK_REVIEW
```

---

## 3. Fluxo End-to-End

```
1. MLP faz upload do PDF no Zora Web App
       ↓
2. loan-document-service → S3 upload → publica FILE_UPSERT
       ↓
3. document-data-extractor-service recebe FILE_UPSERT
   → envia PDF para Ocrolus (async)
   → Ocrolus retorna: { classification: { value: "Conditional Approval" } }
   → publica DOCUMENT_DATA_EXTRACTION_COMPLETED
       ↓
4. loan-document-service cria LoanDocument
   → publica DOCUMENT_UPSERT com payload:
     {
       documentId: string,
       loanApplicationId: string,
       tenantId: string,
       classification: "Conditional Approval",   ← aqui!
       status: "classified"
     }
       ↓
5. agent-service recebe DOCUMENT_UPSERT via SQS (loan-documents-channel)
   consumer: on-document-upserted.ts → processEvent("DOCUMENT_UPSERT", ...)
   1:N routing: R2 + R3 + R6 todos recebem o mesmo evento
       ↓
6. R2 — Tier 1 (O(1), sem LLM):
   payload.classification?.toLowerCase() === "conditional approval" → PASS ✓
       ↓
7. R2 — Tier 2: Mastra Agent (powerful model)
   a. get_document(applicationId, documentId)     → extrai loanFileId
   b. get_file_content(applicationId, loanFileId) → PDF como base64
   c. LLM lê PDF diretamente (Claude/GPT-4o suportam PDF nativo)
   d. prepare_condoval_import(applicationId)       → existing tasks + dedup guide
   e. create_task × N (uma por condição, com conditionDetails)
   f. create_comment → "R2 processou N condições do condval"
       ↓
8. ProposedActions → Confidence Gate
   CREATE_TASK:    threshold=0, autoExecute=true → executa
   CREATE_COMMENT: threshold=0, autoExecute=true → executa
       ↓
9. task-service cria as tasks → publica TASK_CREATED × N
       ↓
10. R7 — Tier 1: task.type === "document" ou "condition" → PASS
        ↓
11. R7 — Tier 2: Mastra Agent
    a. get_task(taskId)         → detalhes da task/condição
    b. list_documents(appId)    → documentos já upados no loan
    c. LLM faz matching semântico: "este doc satisfaz esta condição?"
    d. Se match (confidence ≥ 0.8):
       → update_task({ linkedDocuments: [docId] })
    e. Se sem match:
       → suggest_condition_destinations(...)  → LLM sugere 2-3 destinatários
       → draft_condition_outreach(...)        → cria draft de email por condição
        ↓
12. Agent Inbox: MLP vê tasks criadas, docs linkados, emails rascunhados
    → Revisa, aprova ou edita → envia
```

---

## 4. agent-service — R2 Handler

### 4.1 `src/workflows/definitions.ts` — atualizar R2

```typescript
// Localizar o stub existente do R2 e substituir por:
{
  id: "R2",
  name: "Conditional Approval Processing",
  type: "reactive",
  triggers: ["DOCUMENT_UPSERT"],          // ← único trigger
  enabled: true,
  modelTier: "powerful",                   // GPT-4o ou Claude Sonnet
  tier1: r2Tier1Checks,                    // import de handlers/r2.tier1.ts
  tools: ["get_agent_context", "query_recent_actions"],
  handler: r2Handler,                      // import de handlers/r2.ts
}
```

### 4.2 `src/workflows/handlers/r2.tier1.ts` — CRIAR

```typescript
import type { PipelineContext, Tier1CheckDef } from "@domain/types";

export const r2Tier1Checks: Tier1CheckDef[] = [
  {
    name: "document-classified-as-conditional-approval",
    check: async (context: PipelineContext): Promise<boolean> => {
      const classification = context.event.payload.classification as
        | string
        | undefined;
      // .toLowerCase() para garantir case-insensitive
      return classification?.toLowerCase() === "conditional approval";
    },
  },
];
```

**Por que só um check:**
- O Ocrolus já fez a classificação com ML especializado em mortgage documents
- String equality é O(1), zero custo de LLM, zero ambiguidade
- Falsos positivos são quase impossíveis — Ocrolus é altamente preciso

### 4.3 `src/workflows/handlers/r2.ts` — CRIAR

```typescript
import { logger } from "@common/log";
import { buildSoulPrompt } from "@domain/prompts/soul";
import type { WorkflowHandler } from "@domain/types";
import { Agent } from "@mastra/core/agent";
import {
  parseProposedActions,
  toMastraModel,
  toMastraTools,
} from "@workflows/mastra/adapter";
import { getOrCreateMemory } from "@workflows/mastra/memory";
import { pickTools } from "@workflows/tools";

const OUTPUT_INSTRUCTIONS = `

## Response Format

You MUST respond with a JSON array of proposed actions. Each action:

\`\`\`json
[
  {
    "action": "CREATE_TASK",
    "confidence": 0.95,
    "payload": {
      "applicationId": "uuid-here",
      "title": "Provide most recent pay stub",
      "type": "document",
      "milestone": "condition",
      "conditionDetails": {
        "conditionId": "1",
        "timingCode": "PTD",
        "topic": "Income",
        "lenderCondition": "Provide most recent pay stub for all borrowers"
      }
    },
    "reasoning": "Condition requires pay stub documentation prior to funding",
    "urgency": "high",
    "title": "Condition: provide pay stub",
    "summary": "PTD condition requiring pay stub for borrowers"
  },
  {
    "action": "CREATE_COMMENT",
    "confidence": 1.0,
    "payload": {
      "applicationId": "uuid-here",
      "message": "R2 processed conditional approval: extracted 5 conditions, created 5 tasks."
    },
    "reasoning": "Summary of R2 processing for audit trail",
    "urgency": "low",
    "title": "R2 processing summary",
    "summary": "Conditional approval processed automatically"
  }
]
\`\`\`

If the document is not a conditional approval or no conditions could be extracted, return [].

Rules:
- ALWAYS call prepare_condoval_import BEFORE creating any tasks (dedup)
- NEVER create duplicate tasks — the dedup guide explains how to match
- conditionDetails MUST be populated on every CREATE_TASK
- milestone is always "condition" for condval tasks
- Include ONE CREATE_COMMENT summarizing what was done
`;

export const r2Handler: WorkflowHandler = {
  async execute(context) {
    try {
      const instructions = buildSoulPrompt("r2") + OUTPUT_INSTRUCTIONS;
      const mcpToolDefs = await context.getMcpTools();
      const selectedMcp = pickTools(mcpToolDefs, [
        // Document access
        "get_document",
        "get_file_content",      // ← nova tool — baixa PDF como base64
        // Task management
        "prepare_condoval_import",
        "list_tasks",
        "create_task",
        // Audit
        "create_comment",
        // Loan context
        "get_loan",
        "list_borrowers",
      ]);
      const tools = toMastraTools([...context.tools, ...selectedMcp]);
      const modelId = toMastraModel(context.config.model);
      const memory = getOrCreateMemory();

      const agent = new Agent({
        id: "r2-condoval-processor",
        name: "R2 Conditional Approval Processor",
        model: modelId,
        instructions,
        tools,
        memory,
      });

      const threadId = `r2-${context.loanId ?? context.tenantId}`;

      const documentId = context.event.payload.documentId as string;
      const loanId = context.loanId ?? context.event.payload.loanApplicationId as string;

      const prompt = `
Process the conditional approval document for loan application ${loanId}.
Document ID: ${documentId}

Steps:
1. Call get_document with applicationId="${loanId}" and documentId="${documentId}" to get the loanFileId
2. Call get_file_content with the applicationId and loanFileId to download the PDF as base64
3. Read the PDF carefully — it is a conditional approval letter from the lender
4. Call prepare_condoval_import(applicationId="${loanId}") to get existing tasks and dedup instructions
5. Extract ALL conditions from the PDF following the dedup guide
6. For each NEW condition, propose a CREATE_TASK action with full conditionDetails
7. Propose one CREATE_COMMENT summarizing the processing
      `.trim();

      const result = await agent.generate(prompt, {
        memory: {
          thread: threadId,
          resource: context.tenantId,
        },
        maxSteps: 20,
      });

      return parseProposedActions(result.text);
    } catch (error) {
      logger.error("R2 handler failed", {
        error: error instanceof Error ? error.message : String(error),
        loanId: context.loanId,
        tenantId: context.tenantId,
      });
      return [];
    }
  },
};
```

### 4.4 `src/domain/prompts/workflows/r2.md` — CRIAR

```markdown
# Zora — Conditional Approval Processor (R2)

You are processing a conditional approval document for a mortgage loan.
Your mission: extract ALL conditions from the PDF and turn them into actionable tasks.

## How to Extract Conditions

Step 1: Call `get_document(applicationId, documentId)` to retrieve document metadata.
  - The response includes a `loanFileId` field — you need this for the next step.

Step 2: Call `get_file_content(applicationId, loanFileId)` to download the raw PDF.
  - This returns `{ contentBase64, mimeType, fileName }`.
  - Pass the `contentBase64` as a PDF document to your vision/document reading capability.

Step 3: Read the PDF directly and thoroughly.
  - Look for sections titled "Conditions", "Prior to Documentation (PTD)", "Prior to Funding (PTF)"
  - Look for tables with columns: condition number, timing, category, description
  - Extract EVERY condition — even if the table is long

Step 4: Call `prepare_condoval_import(applicationId)` to get:
  - `existingTasks`: tasks already created from previous condval imports
  - `extractionGuide`: instructions for deduplication and field mapping

Step 5: Follow the `extractionGuide.dedup` instructions CAREFULLY.
  - conditionId is NOT unique — multiple conditions can share the same code
  - Match using the combination of: lenderCondition text (fuzzy), topic, timingCode, conditionId
  - Skip conditions that already have a matching task

## Condition Fields

For each CREATE_TASK action, populate `conditionDetails`:
```json
{
  "conditionId": "1",              // lender's code (NOT unique)
  "timingCode": "PTD",             // PTD | PTF | PTC | UW
  "topic": "Income",               // Income | Assets | Credit | Property | Title | Insurance | Legal | Compliance | Other
  "lenderCondition": "verbatim text from the PDF"  // exact wording, preserve it
}
```

Task `type`:
- `"document"` → condition requires a specific document (pay stub, W2, etc.)
- `"task"` → condition requires an action (verify employment, confirm flood zone, etc.)

Task `milestone`: always `"condition"` for condval tasks.

## Confidence Scoring

- 1.0: clear condition, exact extraction
- 0.85–0.99: high confidence, minor ambiguity in wording
- 0.7–0.84: uncertain — include but flag for review
- < 0.7: skip — don't create ambiguous tasks

## Output Summary

Always end with ONE `CREATE_COMMENT` action summarizing:
- How many conditions were found in the PDF
- How many tasks were created (new)
- How many were skipped (duplicates)
- Any issues encountered

## Important Rules

- NEVER create duplicate tasks — the `extractionGuide.dedup` tells you exactly how to check
- ALWAYS preserve the verbatim `lenderCondition` text — do not paraphrase
- If the PDF is not a conditional approval, return `[]` immediately
- `milestone` is always `"condition"` — never anything else for condval tasks
```

### 4.5 `src/domain/prompts/soul.ts` — registrar r2

Verificar se `buildSoulPrompt("r2")` já lê automaticamente arquivos `.md` pelo ID. Se não, registrar explicitamente:

```typescript
// Em soul.ts, adicionar "r2" ao mapa de prompts (seguir padrão existente do p5)
```

---

## 5. mcp-gateway-service — Novas Tools

### 5.1 `src/tools/documents/query/get-file-content.ts` — CRIAR

```typescript
import type { ApiGatewayClient } from "@clients/api-gateway";
import { logger } from "@common/log";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { toolErrorResponse, toolResponse } from "@tools/common/tool-response";
import type { ZodType } from "zod";
import { z } from "zod/v3";

const inputSchema = z.object({
  applicationId: z
    .string()
    .min(1)
    .describe("Loan application ID"),
  loanFileId: z
    .string()
    .min(1)
    .describe(
      "Loan file ID (obtain from get_document response or list_files). " +
      "This is the physical file ID, distinct from the document ID."
    ),
});

type GetFileContentInput = z.infer<typeof inputSchema>;

export function registerGetFileContentTool(
  server: McpServer,
  apiClient: ApiGatewayClient
): void {
  server.registerTool(
    "get_file_content",
    {
      description:
        "Download the raw content of a loan file (PDF, image, etc.) and return it as base64. " +
        "Use this to pass a file directly to an LLM for reading and analysis. " +
        "Typical use: download the conditional approval PDF to extract conditions. " +
        "Call get_document or list_files first to obtain the loanFileId.",
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
      inputSchema: inputSchema as unknown as ZodType<object>,
    },
    async (rawArgs) => {
      const args = rawArgs as GetFileContentInput;
      const appId = encodeURIComponent(args.applicationId);
      const fileId = encodeURIComponent(args.loanFileId);
      const path = `/loan-document/v1/loan_applications/${appId}/files/${fileId}/download`;

      logger.debug("Executing get_file_content", {
        applicationId: args.applicationId,
        loanFileId: args.loanFileId,
      });

      // Use the raw fetch capability of the API client to get binary data
      const response = await apiClient.getRaw(path);

      if (!response.ok) {
        return toolErrorResponse(response.error, response.status);
      }

      const buffer = Buffer.from(response.data as ArrayBuffer);
      const contentBase64 = buffer.toString("base64");
      const mimeType = response.headers?.["content-type"] ?? "application/pdf";
      const contentDisposition = response.headers?.["content-disposition"] ?? "";
      const fileNameMatch = contentDisposition.match(/filename="?([^";\n]+)"?/);
      const fileName = fileNameMatch?.[1] ?? args.loanFileId;

      return toolResponse({
        contentBase64,
        mimeType,
        fileName,
        sizeBytes: buffer.length,
        encoding: "base64",
        usage: "Pass contentBase64 to the LLM as a PDF document input for direct reading.",
      });
    }
  );
}
```

> **Nota:** Se `ApiGatewayClient` não tiver um método `getRaw` que retorne buffer binário, ele precisa ser adicionado. O método `get` atual provavelmente parseia JSON. Ver `src/clients/api-gateway.ts` para adaptar — adicionar `getRaw(path): Promise<{ ok: boolean; data: ArrayBuffer; headers: Record<string, string>; ... }>`.

**Registrar em `src/tools/documents/index.ts`:**
```typescript
import { registerGetFileContentTool } from "./query/get-file-content";

// Dentro de registerDocumentTools():
registerGetFileContentTool(server, apiClient);
tools.push("get_file_content");
```

---

### 5.2 `src/tools/tasks/query/suggest-condition-destinations.ts` — CRIAR

```typescript
import type { ApiGatewayClient } from "@clients/api-gateway";
import { logger } from "@common/log";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { toolErrorResponse, toolResponse } from "@tools/common/tool-response";
import type { ZodType } from "zod";
import { z } from "zod/v3";

const RecipientTypeSchema = z.enum([
  "borrower",
  "employer",
  "title_company",
  "lender",
  "insurance_provider",
  "appraiser",
  "cpa_accountant",
  "attorney",
  "underwriter",
]);

const ConditionInputSchema = z.object({
  taskId: z.string().describe("Task ID this condition belongs to"),
  conditionText: z.string().describe("Verbatim condition text from the condval"),
  timingCode: z
    .enum(["PTD", "PTF", "PTC", "UW"])
    .describe("Timing code of the condition"),
  topic: z
    .enum([
      "Income", "Assets", "Credit", "Property",
      "Title", "Insurance", "Legal", "Compliance", "Other",
    ])
    .describe("Condition topic/category"),
});

const inputSchema = z.object({
  applicationId: z
    .string()
    .describe("Loan application ID"),
  conditions: z
    .array(ConditionInputSchema)
    .min(1)
    .describe("List of unresolved conditions needing destination suggestions"),
});

type SuggestConditionDestinationsInput = z.infer<typeof inputSchema>;

// Structured output schema — used in the LLM prompt
const STRUCTURED_OUTPUT_SCHEMA = `
{
  "destinations": [
    {
      "taskId": "string",
      "suggestions": [
        {
          "recipientType": "borrower | employer | title_company | lender | insurance_provider | appraiser | cpa_accountant | attorney | underwriter",
          "recipientName": "string",
          "recipientEmail": "string (if known from loan data) | null",
          "confidence": 0.0-1.0,
          "rationale": "brief reason why this recipient is appropriate"
        }
      ]
    }
  ]
}
`;

export function registerSuggestConditionDestinationsTool(
  server: McpServer,
  apiClient: ApiGatewayClient
): void {
  server.registerTool(
    "suggest_condition_destinations",
    {
      description:
        "For each unresolved condition/task, suggest 2-3 likely recipients for outreach emails. " +
        "Uses LLM reasoning with structured output to match condition topics to appropriate contacts " +
        "(borrower, employer, title company, lender, etc.) based on loan data and condition text. " +
        "Call this for conditions that have no matching document uploaded yet.",
      annotations: {
        readOnlyHint: true,
        destructiveHint: false,
        idempotentHint: true,
        openWorldHint: false,
      },
      inputSchema: inputSchema as unknown as ZodType<object>,
    },
    async (rawArgs) => {
      const args = rawArgs as SuggestConditionDestinationsInput;
      const appId = encodeURIComponent(args.applicationId);

      logger.debug("Executing suggest_condition_destinations", {
        applicationId: args.applicationId,
        conditionCount: args.conditions.length,
      });

      // Fetch loan context for the LLM
      const [loanResp, borrowersResp] = await Promise.all([
        apiClient.get(`/loan-application/v1/loan-applications/${appId}`),
        apiClient.get(`/loan-application/v1/loan-applications/${appId}/borrowers`),
      ]);

      if (!loanResp.ok) {
        return toolErrorResponse(loanResp.error, loanResp.status);
      }

      const loanData = loanResp.data;
      const borrowersData = borrowersResp.ok ? borrowersResp.data : { data: [] };

      // Build LLM prompt for structured output
      const prompt = `
You are a mortgage loan processing assistant. For each condition below, suggest 2-3 likely email recipients.

## Loan Context
${JSON.stringify(loanData, null, 2)}

## Borrowers
${JSON.stringify(borrowersData, null, 2)}

## Conditions Needing Recipients
${JSON.stringify(args.conditions, null, 2)}

## Recipient Types (use ONLY these enum values)
- borrower: the loan applicant(s)
- employer: borrower's employer (for income verification)
- title_company: title/escrow company
- lender: the lending institution
- insurance_provider: homeowner's or other insurance provider
- appraiser: property appraiser
- cpa_accountant: CPA or tax accountant (for tax/income docs)
- attorney: legal counsel
- underwriter: underwriter at the lender

## Instructions
For each condition, reason about who should provide or confirm the required information.
Consider the topic, timing code, and verbatim condition text.
Return ONLY valid JSON matching this schema:
${STRUCTURED_OUTPUT_SCHEMA}
      `.trim();

      // Invoke LLM via Orq or direct (follow existing pattern in the codebase)
      // TODO: Use the configured LLM client (Orq.ai or OpenAI) for structured output
      // The response must be valid JSON matching STRUCTURED_OUTPUT_SCHEMA
      //
      // Example using Orq.ai deployment:
      // const llmResponse = await orqClient.invokeDeployment("suggest-condition-destinations", { prompt });
      // const parsed = JSON.parse(llmResponse.text);

      // Placeholder — replace with actual LLM call:
      return toolResponse({
        _note: "LLM call to be implemented using the project's configured LLM client (Orq.ai)",
        prompt,
        structuredOutputSchema: STRUCTURED_OUTPUT_SCHEMA,
      });
    }
  );
}
```

> **Nota de implementação:** A chamada ao LLM deve usar o cliente já configurado no projeto (Orq.ai ou OpenAI direto). Ver como `communication-service/src/services/orq/index.ts` invoca deployments. O parâmetro `structured_output` ou JSON mode deve ser habilitado para garantir output válido.

**Registrar em `src/tools/tasks/index.ts`:**
```typescript
import { registerSuggestConditionDestinationsTool } from "./query/suggest-condition-destinations";

// Dentro de registerTaskTools():
registerSuggestConditionDestinationsTool(server, apiClient);
tools.push("suggest_condition_destinations");
```

---

### 5.3 `src/tools/emails/mutation/draft-condition-outreach.ts` — CRIAR

```typescript
import type { AuthContext } from "@auth/types";
import type { ApiGatewayClient } from "@clients/api-gateway";
import { logger } from "@common/log";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { toolErrorResponse, toolResponse } from "@tools/common/tool-response";
import type { ZodType } from "zod";
import { z } from "zod/v3";

const RecipientTypeSchema = z.enum([
  "borrower", "employer", "title_company", "lender",
  "insurance_provider", "appraiser", "cpa_accountant", "attorney", "underwriter",
]);

const inputSchema = z.object({
  applicationId: z.string().uuid().describe("Loan application ID"),
  taskId: z.string().describe("Task ID this outreach is for"),
  conditionText: z.string().describe("Verbatim condition text from the condval"),
  recipientType: RecipientTypeSchema.describe("Type of recipient"),
  recipientName: z.string().describe("Recipient display name"),
  recipientEmail: z.string().email().describe("Recipient email address"),
  confidenceScore: z
    .number()
    .min(0)
    .max(1)
    .describe("Confidence score from suggest_condition_destinations (0-1)"),
  subject: z
    .string()
    .optional()
    .describe("Email subject (auto-generated if not provided)"),
  body: z
    .string()
    .optional()
    .describe("Email body HTML (auto-generated if not provided)"),
});

type DraftConditionOutreachInput = z.infer<typeof inputSchema>;

export function registerDraftConditionOutreachTool(
  server: McpServer,
  authContext: AuthContext,
  apiClient: ApiGatewayClient
): void {
  server.registerTool(
    "draft_condition_outreach",
    {
      description:
        "Create a draft outreach email for a specific loan condition. " +
        "Unlike draft_email (which manages a single title-order email), this tool " +
        "creates one draft per condition/recipient pair, stored in condition_outreach_drafts. " +
        "Call suggest_condition_destinations first to get the recipient. " +
        "WARNING: Not idempotent — check existing drafts before calling.",
      annotations: {
        readOnlyHint: false,
        destructiveHint: false,
        idempotentHint: false,
        openWorldHint: false,
      },
      inputSchema: inputSchema as unknown as ZodType<object>,
    },
    async (rawArgs) => {
      const args = rawArgs as DraftConditionOutreachInput;
      const path = "/communication/v1/condition-outreach-drafts";

      logger.debug("Executing draft_condition_outreach", {
        applicationId: args.applicationId,
        taskId: args.taskId,
        recipientType: args.recipientType,
        tenantId: authContext.tenantId,
      });

      const body = {
        data: {
          type: "condition-outreach-draft",
          attributes: {
            loanApplicationId: args.applicationId,
            taskId: args.taskId,
            conditionText: args.conditionText,
            recipientType: args.recipientType,
            recipientName: args.recipientName,
            recipientEmail: args.recipientEmail,
            confidenceScore: args.confidenceScore,
            subject: args.subject,
            body: args.body,
            tenantId: authContext.tenantId,
            createdByAgentWorkflow: "R2",
            status: "draft",
          },
        },
      };

      const response = await apiClient.post(path, body, {
        "x-hn-operation": "mcp.draft_condition_outreach",
      });

      if (!response.ok) {
        return toolErrorResponse(response.error, response.status);
      }

      return toolResponse(response.data);
    }
  );
}
```

**Registrar em `src/tools/emails/index.ts`:**
```typescript
import { registerDraftConditionOutreachTool } from "./mutation/draft-condition-outreach";

// Dentro de registerEmailTools(), com scope emails:write:
if (authContext.scopes.includes("emails:write")) {
  registerDraftConditionOutreachTool(server, authContext, apiClient);
  tools.push("draft_condition_outreach");
}
```

---

## 6. communication-service — collection condition_outreach_drafts

### 6.1 `src/lib/mongoose/connection.ts` — ATUALIZAR

```typescript
// Adicionar ao objeto AVAILABLE_CONNECTION:
const AVAILABLE_CONNECTION = {
  frontMessages: "FRONT_MESSAGES",
  frontConversations: "FRONT_CONVERSATIONS",
  triageOperations: "TRIAGE_OPERATIONS",
  comments: "COMMENTS",
  conditionOutreachDrafts: "CONDITION_OUTREACH_DRAFTS",  // ← adicionar
} as const;
```

```typescript
// Adicionar em getDbSettings():
[AVAILABLE_CONNECTION.conditionOutreachDrafts]: {
  url: env.MONGO_CONDITION_OUTREACH_DRAFTS_DB_URL,
  dbName: env.MONGO_CONDITION_OUTREACH_DRAFTS_DB_NAME,
  keyAltNames: env.HN_MONGO_CRYPT_KEY_ALT_NAMES.split(","),
},
```

---

### 6.2 `src/schemas/condition-outreach-draft.schema.ts` — CRIAR

```typescript
import { z } from "zod";

export const RecipientTypeSchema = z.enum([
  "borrower",
  "employer",
  "title_company",
  "lender",
  "insurance_provider",
  "appraiser",
  "cpa_accountant",
  "attorney",
  "underwriter",
]);

export const ConditionOutreachDraftStatusSchema = z.enum([
  "draft",
  "sent",
  "dismissed",
]);

export const ConditionOutreachDraftAttributesSchema = z.object({
  loanApplicationId: z.string(),
  taskId: z.string(),
  conditionText: z.string(),
  recipientType: RecipientTypeSchema,
  recipientName: z.string(),
  recipientEmail: z.string().email(),
  subject: z.string(),
  body: z.string(),
  status: ConditionOutreachDraftStatusSchema,
  confidenceScore: z.number().min(0).max(1),
  tenantId: z.string(),
  createdByAgentWorkflow: z.string(),
  sentAt: z.date().optional(),
  dismissedAt: z.date().optional(),
});

export const ConditionOutreachDraftSchema = z.object({
  draftId: z.string().uuid(),
  attributes: ConditionOutreachDraftAttributesSchema,
  createdAt: z.date().optional(),
  updatedAt: z.date().optional(),
  deletedAt: z.date().optional(),
});

export type RecipientType = z.infer<typeof RecipientTypeSchema>;
export type ConditionOutreachDraftStatus = z.infer<typeof ConditionOutreachDraftStatusSchema>;
export type ConditionOutreachDraftAttributes = z.infer<typeof ConditionOutreachDraftAttributesSchema>;
export type ConditionOutreachDraft = z.infer<typeof ConditionOutreachDraftSchema>;
export type ConditionOutreachDraftCreate = Omit<
  ConditionOutreachDraft,
  "draftId" | "createdAt" | "updatedAt" | "deletedAt"
>;
```

---

### 6.3 `src/interfaces/models/i-condition-outreach-draft.ts` — CRIAR

```typescript
// Tipos derivados do Zod schema — NÃO duplicar definições
import type {
  ConditionOutreachDraft,
  ConditionOutreachDraftCreate,
  ConditionOutreachDraftAttributes,
} from "../../schemas/condition-outreach-draft.schema";

// Re-export para uso nos repositories e use-cases
export type IConditionOutreachDraft = ConditionOutreachDraft;
export type IConditionOutreachDraftCreate = ConditionOutreachDraftCreate;
export type IConditionOutreachDraftAttributes = ConditionOutreachDraftAttributes;
```

---

### 6.4 `src/interfaces/repositories/i-condition-outreach-draft-repository.ts` — CRIAR

```typescript
import type { IConditionOutreachDraft, IConditionOutreachDraftCreate } from "../models/i-condition-outreach-draft";

export interface IConditionOutreachDraftRepository {
  create(data: IConditionOutreachDraftCreate): Promise<IConditionOutreachDraft>;
  findById(draftId: string): Promise<IConditionOutreachDraft | null>;
  findByLoanApplicationId(
    loanApplicationId: string,
    tenantId: string
  ): Promise<IConditionOutreachDraft[]>;
  findByTaskId(taskId: string): Promise<IConditionOutreachDraft[]>;
  update(
    draftId: string,
    data: Partial<IConditionOutreachDraftCreate["attributes"]>
  ): Promise<IConditionOutreachDraft>;
  updateStatus(
    draftId: string,
    status: "draft" | "sent" | "dismissed",
    at?: Date
  ): Promise<void>;
  softDelete(draftId: string): Promise<void>;
}
```

---

### 6.5 `src/db/condition-outreach-draft/model.ts` — CRIAR

```typescript
import type { IConditionOutreachDraft } from "@interfaces/models/i-condition-outreach-draft";
import { db } from "@lib/mongoose";
import { AVAILABLE_CONNECTION } from "@lib/mongoose/connection";
import { Schema } from "mongoose";
import { v4 as uuidv4 } from "uuid";

const COLLECTION_NAME = "conditionOutreachDrafts";

// CRITICAL: Schema-first — ALL business data in attributes as Mixed
const conditionOutreachDraftSchema = new Schema<IConditionOutreachDraft>(
  {
    draftId: {
      type: String,
      unique: true,
      required: true,
      default: () => uuidv4(),
    },
    attributes: {
      type: Schema.Types.Mixed,
      default: {},
    },
    deletedAt: { type: Date },
  },
  {
    timestamps: true,
    minimize: false,
    strict: false,
  }
);

// Business indexes — only for frequently queried fields INSIDE attributes
conditionOutreachDraftSchema.index({ "attributes.loanApplicationId": 1 });
conditionOutreachDraftSchema.index({ "attributes.taskId": 1 });
conditionOutreachDraftSchema.index({ "attributes.tenantId": 1, createdAt: -1 });
conditionOutreachDraftSchema.index({ "attributes.status": 1 });
// Compound: find drafts by loan + status
conditionOutreachDraftSchema.index({
  "attributes.loanApplicationId": 1,
  "attributes.status": 1,
});

// CRITICAL: Mark attributes as modified on save (Schema.Types.Mixed requirement)
conditionOutreachDraftSchema.pre("save", function (next) {
  if (this.isModified("attributes")) {
    this.markModified("attributes");
  }
  next();
});

async function getModel() {
  return await db.getModel<IConditionOutreachDraft>(
    AVAILABLE_CONNECTION.conditionOutreachDrafts,
    COLLECTION_NAME,
    conditionOutreachDraftSchema
  );
}

export { getModel };
```

---

### 6.6 `src/repositories/condition-outreach-draft-repository.ts` — CRIAR

```typescript
import type { Logger } from "@housenumber/logger";
import { ConditionOutreachDraftSchema } from "@schemas/condition-outreach-draft.schema";
import { parseErrorForLogging } from "@utils/error-parser";
import { getModel } from "../db/condition-outreach-draft/model";
import type {
  IConditionOutreachDraft,
  IConditionOutreachDraftCreate,
} from "../interfaces/models/i-condition-outreach-draft";
import type { IConditionOutreachDraftRepository } from "../interfaces/repositories/i-condition-outreach-draft-repository";

export class ConditionOutreachDraftRepository
  implements IConditionOutreachDraftRepository
{
  private readonly deps: { currentDate: Date; logger: Logger };

  constructor(deps: { currentDate: Date; logger: Logger }) {
    this.deps = deps;
  }

  private getModel() {
    return getModel();
  }

  async create(
    data: IConditionOutreachDraftCreate
  ): Promise<IConditionOutreachDraft> {
    const { currentDate, logger } = this.deps;

    try {
      const Model = await this.getModel();
      const doc = await Model.create({
        ...data,
        createdAt: currentDate,
        updatedAt: currentDate,
      });

      logger.info("Condition outreach draft created", {
        draftId: doc.draftId,
        taskId: data.attributes.taskId,
      });

      // Validate via Zod — .toObject() returns any internally
      return ConditionOutreachDraftSchema.parse(doc.toObject());
    } catch (error) {
      logger.error("Failed to create condition outreach draft", {
        ...parseErrorForLogging(error),
      });
      throw error;
    }
  }

  async findById(draftId: string): Promise<IConditionOutreachDraft | null> {
    const { logger } = this.deps;

    try {
      const Model = await this.getModel();
      const doc = await Model.findOne({ draftId, deletedAt: null }).lean();

      if (!doc) {
        return null;
      }

      // Validate via Zod
      return ConditionOutreachDraftSchema.parse(doc);
    } catch (error) {
      logger.error("Failed to find condition outreach draft", {
        draftId,
        ...parseErrorForLogging(error),
      });
      throw error;
    }
  }

  async findByLoanApplicationId(
    loanApplicationId: string,
    tenantId: string
  ): Promise<IConditionOutreachDraft[]> {
    const { logger } = this.deps;

    try {
      const Model = await this.getModel();
      const docs = await Model.find({
        "attributes.loanApplicationId": loanApplicationId,
        "attributes.tenantId": tenantId,
        deletedAt: null,
      })
        .sort({ createdAt: -1 })
        .lean();

      // Validate each doc via Zod
      return docs.map((doc) => ConditionOutreachDraftSchema.parse(doc));
    } catch (error) {
      logger.error("Failed to list condition outreach drafts by loan", {
        loanApplicationId,
        ...parseErrorForLogging(error),
      });
      throw error;
    }
  }

  async findByTaskId(taskId: string): Promise<IConditionOutreachDraft[]> {
    const { logger } = this.deps;

    try {
      const Model = await this.getModel();
      const docs = await Model.find({
        "attributes.taskId": taskId,
        deletedAt: null,
      }).lean();

      return docs.map((doc) => ConditionOutreachDraftSchema.parse(doc));
    } catch (error) {
      logger.error("Failed to list condition outreach drafts by task", {
        taskId,
        ...parseErrorForLogging(error),
      });
      throw error;
    }
  }

  async update(
    draftId: string,
    data: Partial<IConditionOutreachDraftCreate["attributes"]>
  ): Promise<IConditionOutreachDraft> {
    const { currentDate, logger } = this.deps;

    try {
      const Model = await this.getModel();
      const updateFields = Object.entries(data).reduce(
        (acc, [key, value]) => {
          acc[`attributes.${key}`] = value;
          return acc;
        },
        {} as Record<string, unknown>
      );

      const doc = await Model.findOneAndUpdate(
        { draftId, deletedAt: null },
        { $set: { ...updateFields, updatedAt: currentDate } },
        { new: true }
      ).lean();

      if (!doc) {
        throw new Error(`Condition outreach draft not found: ${draftId}`);
      }

      return ConditionOutreachDraftSchema.parse(doc);
    } catch (error) {
      logger.error("Failed to update condition outreach draft", {
        draftId,
        ...parseErrorForLogging(error),
      });
      throw error;
    }
  }

  async updateStatus(
    draftId: string,
    status: "draft" | "sent" | "dismissed",
    at?: Date
  ): Promise<void> {
    const { currentDate, logger } = this.deps;

    try {
      const Model = await this.getModel();
      const update: Record<string, unknown> = {
        "attributes.status": status,
        updatedAt: currentDate,
      };

      if (status === "sent" && at) {
        update["attributes.sentAt"] = at;
      }
      if (status === "dismissed" && at) {
        update["attributes.dismissedAt"] = at;
      }

      await Model.updateOne({ draftId }, { $set: update });

      logger.info("Condition outreach draft status updated", { draftId, status });
    } catch (error) {
      logger.error("Failed to update condition outreach draft status", {
        draftId,
        status,
        ...parseErrorForLogging(error),
      });
      throw error;
    }
  }

  async softDelete(draftId: string): Promise<void> {
    const { currentDate, logger } = this.deps;

    try {
      const Model = await this.getModel();
      await Model.updateOne(
        { draftId },
        { $set: { deletedAt: currentDate, updatedAt: currentDate } }
      );

      logger.info("Condition outreach draft soft-deleted", { draftId });
    } catch (error) {
      logger.error("Failed to soft-delete condition outreach draft", {
        draftId,
        ...parseErrorForLogging(error),
      });
      throw error;
    }
  }
}
```

---

### 6.7 REST Endpoints — `src/routes/v1/condition-outreach-drafts/` — CRIAR

Seguir o padrão de `src/routes/v1/` existente (use-case pattern + JSON:API).

**`index.ts`** — router principal:
```typescript
// POST   /v1/condition-outreach-drafts        → create
// GET    /v1/condition-outreach-drafts?loanApplicationId=&tenantId=  → list
// GET    /v1/condition-outreach-drafts/:draftId  → get by ID
// PATCH  /v1/condition-outreach-drafts/:draftId  → update
// DELETE /v1/condition-outreach-drafts/:draftId  → soft delete
```

Cada endpoint:
1. Lê `tenantId` do header (`x-hn-tenant-id` ou equivalente)
2. Instancia `ConditionOutreachDraftRepository({ currentDate, logger })`
3. Instancia o use-case correspondente
4. Retorna JSON:API format

**Use-cases a criar:**
- `use-cases/create-condition-outreach-draft.ts`
- `use-cases/list-condition-outreach-drafts.ts`
- `use-cases/get-condition-outreach-draft.ts`
- `use-cases/update-condition-outreach-draft.ts`
- `use-cases/delete-condition-outreach-draft.ts`

Registrar o router em `src/app.ts`:
```typescript
import conditionOutreachDraftsRouter from "./routes/v1/condition-outreach-drafts";
app.use("/v1/condition-outreach-drafts", conditionOutreachDraftsRouter);
```

---

### 6.8 api-gateway-service routing — ATUALIZAR

No `api-gateway-service`, adicionar rota para proxiar requests ao `communication-service`:

```
/communication/v1/condition-outreach-drafts → communication-service:PORT/v1/condition-outreach-drafts
```

Ver como outras rotas do `communication-service` são registradas no api-gateway (provavelmente em um arquivo de configuração de rotas ou proxy middleware).

---

## 7. agent-service — R7 Handler (Document Task Auto-Match)

### 7.1 `src/workflows/definitions.ts` — habilitar R7

```typescript
{
  id: "R7",
  name: "Document Task Auto-Match",
  type: "reactive",
  triggers: ["TASK_CREATED", "TASK_UPDATED"],
  enabled: true,
  modelTier: "balanced",            // não precisa de powerful — matching semântico
  tier1: r7Tier1Checks,
  handler: r7Handler,
}
```

### 7.2 `src/workflows/handlers/r7.tier1.ts` — CRIAR

```typescript
import type { PipelineContext, Tier1CheckDef } from "@domain/types";

export const r7Tier1Checks: Tier1CheckDef[] = [
  {
    name: "task-is-condition-type",
    check: async (context: PipelineContext): Promise<boolean> => {
      // Só processa tasks do tipo condition (criadas pelo R2)
      const taskType = context.event.payload.taskType as string | undefined;
      // taskType "document" ou "task" de milestone "condition"
      // O evento TASK_CREATED pode incluir taskType no payload
      // Verificar o contrato real do evento em domain/events.ts
      return taskType === "document" || taskType === "task";
    },
  },
];
```

> **Nota:** Verificar o campo `taskType` no `TaskCreatedEventSchema` em `domain/events.ts`. Se não estiver disponível no payload, considerar usar `get_task(taskId)` no Tier 1 (com custo de API call) ou remover o Tier 1 e deixar o handler fazer a verificação internamente.

### 7.3 `src/workflows/handlers/r7.ts` — CRIAR

```typescript
import { logger } from "@common/log";
import { buildSoulPrompt } from "@domain/prompts/soul";
import type { WorkflowHandler } from "@domain/types";
import { Agent } from "@mastra/core/agent";
import {
  parseProposedActions,
  toMastraModel,
  toMastraTools,
} from "@workflows/mastra/adapter";
import { getOrCreateMemory } from "@workflows/mastra/memory";
import { pickTools } from "@workflows/tools";

const OUTPUT_INSTRUCTIONS = `

## Response Format

Return a JSON array of proposed actions:

\`\`\`json
[
  {
    "action": "UPDATE_TASK_REVIEW",
    "confidence": 0.85,
    "payload": {
      "applicationId": "...",
      "taskId": "...",
      "linkedDocuments": ["doc-id-1"]
    },
    "reasoning": "Uploaded pay stub matches 'Provide most recent pay stub' condition",
    "urgency": "medium",
    "title": "Document match: pay stub linked to task",
    "summary": "Pay stub document matched and linked to condition task"
  },
  {
    "action": "DRAFT_EMAIL",
    "confidence": 0.9,
    "payload": {
      "applicationId": "...",
      "taskId": "...",
      "conditionText": "verbatim condition text",
      "recipientType": "borrower",
      "recipientName": "John Doe",
      "recipientEmail": "john@example.com",
      "confidenceScore": 0.9
    },
    "reasoning": "No matching document found — draft outreach to borrower for income doc",
    "urgency": "high",
    "title": "Outreach draft: income doc request",
    "summary": "Draft email to borrower requesting income documentation"
  }
]
\`\`\`

If the task is already resolved or not a condition task, return [].
`;

export const r7Handler: WorkflowHandler = {
  async execute(context) {
    try {
      const instructions = buildSoulPrompt("r7") + OUTPUT_INSTRUCTIONS;
      const mcpToolDefs = await context.getMcpTools();
      const selectedMcp = pickTools(mcpToolDefs, [
        // Task data
        "get_task",
        "update_task",
        // Document matching
        "list_documents",
        "get_document",
        // Destination suggestion + outreach
        "suggest_condition_destinations",
        "draft_condition_outreach",
        // Loan context
        "get_loan",
        "list_borrowers",
      ]);
      const tools = toMastraTools([...context.tools, ...selectedMcp]);
      const modelId = toMastraModel(context.config.model);
      const memory = getOrCreateMemory();

      const agent = new Agent({
        id: "r7-doc-task-matcher",
        name: "R7 Document Task Auto-Match",
        model: modelId,
        instructions,
        tools,
        memory,
      });

      const taskId = context.event.payload.taskId as string;
      const loanId = context.loanId ?? context.event.payload.loanApplicationId as string;

      const prompt = `
Match documents to the newly created condition task for loan ${loanId}.
Task ID: ${taskId}

Steps:
1. Call get_task(taskId="${taskId}") to get the condition details
2. Call list_documents(applicationId="${loanId}") to see all uploaded documents
3. Semantically match each document against the condition text
   - Confidence ≥ 0.8: propose UPDATE_TASK_REVIEW with linkedDocuments
   - Confidence < 0.8: no match
4. If NO document matches:
   a. Call suggest_condition_destinations with the condition details
   b. For each suggestion (up to 2-3): propose DRAFT_EMAIL action
5. Return [] if the task is already resolved or not a condition task
      `.trim();

      const result = await agent.generate(prompt, {
        memory: {
          thread: `r7-${taskId}`,
          resource: context.tenantId,
        },
        maxSteps: 10,
      });

      return parseProposedActions(result.text);
    } catch (error) {
      logger.error("R7 handler failed", {
        error: error instanceof Error ? error.message : String(error),
        loanId: context.loanId,
        tenantId: context.tenantId,
      });
      return [];
    }
  },
};
```

### 7.4 `src/domain/prompts/workflows/r7.md` — CRIAR

```markdown
# Zora — Document Task Auto-Matcher (R7)

You match uploaded documents to open condition tasks.
For unmatched conditions, you suggest recipients and draft outreach emails.

## Matching Logic

Given a condition task and a list of uploaded documents:

1. Read the condition's `lenderCondition` (verbatim text) and `topic`
2. For each uploaded document, assess: "Does this document satisfy this condition?"
   - Consider document type, classification, and any extracted metadata
   - A pay stub satisfies "Provide most recent pay stub for all borrowers"
   - A W-2 satisfies "Provide W-2 for the last 2 years"
   - A bank statement satisfies "Provide 2 months bank statements"

## Confidence Thresholds

- ≥ 0.80: Match → propose UPDATE_TASK_REVIEW with linkedDocuments
- < 0.80: No match → proceed to destination suggestion

## For Unmatched Conditions

1. Call `suggest_condition_destinations` with the condition details
2. For each suggestion with confidence ≥ 0.6:
   - Propose a DRAFT_EMAIL action (maps to draft_condition_outreach call)
   - Maximum 3 suggestions per condition

## Output Rules

- UPDATE_TASK_REVIEW: links the document to the task (does NOT complete the task — MLP reviews)
- DRAFT_EMAIL: creates a draft outreach email in condition_outreach_drafts
- Never auto-complete tasks (UPDATE_TASK_COMPLETE requires confidence ≥ 0.95 and human review)
- If task is already done/resolved, return []
```

---

## 8. Confidence Gates

### Gates existentes (relevantes para R2/R7)

```typescript
// src/domain/confidence.ts — verificar se estes já existem ou precisam ser adicionados
{
  action: "CREATE_TASK",
  threshold: 0,
  autoExecute: true,
  fallback: "skip",
}
{
  action: "UPDATE_TASK_REVIEW",    // link documento sem completar
  threshold: 0,
  autoExecute: true,
  fallback: "skip",
}
{
  action: "UPDATE_TASK_COMPLETE",  // completar task — requer alta confiança
  threshold: 0.95,
  autoExecute: true,
  fallback: "inbox_review",
}
{
  action: "DRAFT_EMAIL",
  threshold: 0,
  autoExecute: true,
  fallback: "skip",
}
{
  action: "SEND_EMAIL",
  threshold: 0.95,
  autoExecute: true,
  fallback: "draft",
}
{
  action: "CREATE_COMMENT",
  threshold: 0,
  autoExecute: true,
  fallback: "skip",
}
```

### Novos action types a adicionar (se não existirem)

Verificar se `"DRAFT_EMAIL"` no contexto de `draft_condition_outreach` já está coberto. Se não, adicionar um gate específico:

```typescript
{
  action: "DRAFT_CONDITION_OUTREACH",
  threshold: 0.6,    // só drafta se confiança ≥ 0.6 na sugestão de destinatário
  autoExecute: true,
  fallback: "skip",
}
```

---

## 9. Plano de Testes

### 9.1 Testes unitários do handler R2

```typescript
// src/workflows/handlers/r2.spec.ts
import { describe, it, expect, vi } from "vitest";
import { r2Handler } from "./r2";
import { buildMockContext } from "@tests/helpers/pipeline-context";

describe("r2Handler", () => {
  it("retorna CREATE_TASK actions para condições extraídas do PDF", async () => {
    const context = buildMockContext({
      event: {
        type: "DOCUMENT_UPSERT",
        payload: {
          documentId: "doc-123",
          loanApplicationId: "loan-456",
          tenantId: "tenant-789",
          classification: "Conditional Approval",
        },
      },
    });

    const actions = await r2Handler.execute(context);
    expect(actions.some(a => a.action === "CREATE_TASK")).toBe(true);
    expect(actions.every(a => a.confidence >= 0 && a.confidence <= 1)).toBe(true);
  });

  it("retorna [] se nenhuma condição encontrada no PDF", async () => {
    // Mock do agente retornando []
    const context = buildMockContext({ ... });
    const actions = await r2Handler.execute(context);
    expect(actions).toEqual([]);
  });

  it("não cria tasks duplicatas (prepare_condoval_import retorna existing)", async () => {
    // Verificar que o agente respeita o dedup guide
  });
});
```

### 9.2 Testes unitários do Tier 1

```typescript
// src/workflows/handlers/r2.tier1.spec.ts (ou r2.spec.ts)
import { r2Tier1Checks } from "./r2.tier1";
import { buildMockContext } from "@tests/helpers/pipeline-context";

describe("r2Tier1Checks", () => {
  const check = r2Tier1Checks[0];

  it('retorna true para classification "Conditional Approval"', async () => {
    const ctx = buildMockContext({ event: { payload: { classification: "Conditional Approval" } } });
    expect(await check.check(ctx)).toBe(true);
  });

  it('retorna true para classification "conditional approval" (lowercase)', async () => {
    const ctx = buildMockContext({ event: { payload: { classification: "conditional approval" } } });
    expect(await check.check(ctx)).toBe(true);
  });

  it('retorna true para classification "CONDITIONAL APPROVAL" (uppercase)', async () => {
    const ctx = buildMockContext({ event: { payload: { classification: "CONDITIONAL APPROVAL" } } });
    expect(await check.check(ctx)).toBe(true);
  });

  it('retorna false para classification "Pay Stub"', async () => {
    const ctx = buildMockContext({ event: { payload: { classification: "Pay Stub" } } });
    expect(await check.check(ctx)).toBe(false);
  });

  it("retorna false quando classification é undefined", async () => {
    const ctx = buildMockContext({ event: { payload: {} } });
    expect(await check.check(ctx)).toBe(false);
  });
});
```

### 9.3 Testes das novas MCP tools

```typescript
// src/tools/documents/query/__tests__/get-file-content.spec.ts
// Testar: retorna base64 correto, lida com erros 404, 500
// Mock: ApiGatewayClient.getRaw()

// src/tools/tasks/query/__tests__/suggest-condition-destinations.spec.ts
// Testar: input validation, estrutura do output, erros de API

// src/tools/emails/mutation/__tests__/draft-condition-outreach.spec.ts
// Testar: cria draft com campos corretos, erros da API de communication-service
```

### 9.4 Testes do repository

```typescript
// communication-service/src/repositories/__tests__/condition-outreach-draft-repository.spec.ts
// Testar: create, findById, findByLoanApplicationId, findByTaskId, update, updateStatus, softDelete
// Importante: testar que Zod lança erro se MongoDB retornar dados inválidos
// (simular doc corrompido no mock do Model)
```

### 9.5 Teste manual via Trigger API

```bash
# POST /v1/trigger no agent-service
curl -X POST http://localhost:3000/v1/trigger \
  -H "Content-Type: application/json" \
  -H "x-hn-tenant-id: <tenantId>" \
  -d '{
    "workflowId": "R2",
    "payload": {
      "documentId": "<docId>",
      "loanApplicationId": "<loanId>",
      "tenantId": "<tenantId>",
      "classification": "Conditional Approval"
    }
  }'
```

### 9.6 Teste E2E

1. Upload de um PDF de conditional approval real (anonimizado) no ambiente de dev
2. Verificar: Ocrolus classifica como `"Conditional Approval"`
3. Verificar: `DOCUMENT_UPSERT` publicado com `classification: "Conditional Approval"`
4. Verificar: R2 Tier 1 passa, Tier 2 executa
5. Verificar: tasks criadas com `conditionDetails` preenchido
6. Verificar: R7 dispara via `TASK_CREATED`
7. Verificar: documentos linkados às tasks quando há match
8. Verificar: drafts de email criados em `condition_outreach_drafts`
9. Verificar: items aparecendo no Agent Inbox do MLP

---

## 10. Mapa Completo de Arquivos

### agent-service

| Arquivo | Ação | Descrição |
|---|---|---|
| `src/workflows/definitions.ts` | ✏️ Editar | Habilitar R2 (trigger `DOCUMENT_UPSERT`) e R7 (trigger `TASK_CREATED`) |
| `src/workflows/handlers/r2.ts` | 🆕 Criar | Handler do R2 |
| `src/workflows/handlers/r2.tier1.ts` | 🆕 Criar | Tier 1 do R2 |
| `src/workflows/handlers/r2.spec.ts` | 🆕 Criar | Testes do R2 |
| `src/workflows/handlers/r7.ts` | 🆕 Criar | Handler do R7 |
| `src/workflows/handlers/r7.tier1.ts` | 🆕 Criar | Tier 1 do R7 |
| `src/workflows/handlers/r7.spec.ts` | 🆕 Criar | Testes do R7 |
| `src/domain/prompts/workflows/r2.md` | 🆕 Criar | Soul prompt do R2 |
| `src/domain/prompts/workflows/r7.md` | 🆕 Criar | Soul prompt do R7 |
| `src/domain/confidence.ts` | ✏️ Verificar | Adicionar `DRAFT_CONDITION_OUTREACH` se necessário |
| `src/domain/events.ts` | ✏️ Verificar | Confirmar que `classification` está no `DocumentUpsertEventSchema` |

### mcp-gateway-service

| Arquivo | Ação | Descrição |
|---|---|---|
| `src/tools/documents/query/get-file-content.ts` | 🆕 Criar | Tool para baixar PDF como base64 |
| `src/tools/documents/query/__tests__/get-file-content.spec.ts` | 🆕 Criar | Testes |
| `src/tools/documents/index.ts` | ✏️ Editar | Registrar `get_file_content` |
| `src/tools/tasks/query/suggest-condition-destinations.ts` | 🆕 Criar | Tool LLM + structured output |
| `src/tools/tasks/query/__tests__/suggest-condition-destinations.spec.ts` | 🆕 Criar | Testes |
| `src/tools/tasks/index.ts` | ✏️ Editar | Registrar `suggest_condition_destinations` |
| `src/tools/emails/mutation/draft-condition-outreach.ts` | 🆕 Criar | Tool para criar draft de outreach |
| `src/tools/emails/mutation/__tests__/draft-condition-outreach.spec.ts` | 🆕 Criar | Testes |
| `src/tools/emails/index.ts` | ✏️ Editar | Registrar `draft_condition_outreach` |
| `src/clients/api-gateway.ts` | ✏️ Verificar | Adicionar `getRaw()` para download binário |

### communication-service

| Arquivo | Ação | Descrição |
|---|---|---|
| `src/lib/mongoose/connection.ts` | ✏️ Editar | Adicionar `conditionOutreachDrafts` ao `AVAILABLE_CONNECTION` |
| `src/schemas/condition-outreach-draft.schema.ts` | 🆕 Criar | Zod schema (fonte de verdade) |
| `src/schemas/__tests__/condition-outreach-draft.schema.spec.ts` | 🆕 Criar | Testes do schema |
| `src/interfaces/models/i-condition-outreach-draft.ts` | 🆕 Criar | Tipos derivados do schema |
| `src/interfaces/repositories/i-condition-outreach-draft-repository.ts` | 🆕 Criar | Interface do repository |
| `src/db/condition-outreach-draft/model.ts` | 🆕 Criar | Mongoose model |
| `src/repositories/condition-outreach-draft-repository.ts` | 🆕 Criar | Repository com Zod validation |
| `src/repositories/__tests__/condition-outreach-draft-repository.spec.ts` | 🆕 Criar | Testes do repository |
| `src/routes/v1/condition-outreach-drafts/index.ts` | 🆕 Criar | Router Express |
| `src/routes/v1/condition-outreach-drafts/use-cases/create-*.ts` | 🆕 Criar | Use-cases (5 total) |
| `src/app.ts` | ✏️ Editar | Registrar o novo router |
| `src/common/environment/types.ts` | ✏️ Editar | Adicionar novas env vars |

### api-gateway-service

| Arquivo | Ação | Descrição |
|---|---|---|
| Arquivo de rotas | ✏️ Editar | Adicionar proxy `/communication/v1/condition-outreach-drafts` |

---

## 11. Variáveis de Ambiente

### communication-service (novas)

```bash
# MongoDB para a nova collection
MONGO_CONDITION_OUTREACH_DRAFTS_DB_URL=mongodb://...
MONGO_CONDITION_OUTREACH_DRAFTS_DB_NAME=condition-outreach-drafts

# Estas já devem existir (compartilhadas):
HN_MONGO_CRYPT_KEY_ALT_NAMES=...
HN_MONGO_CRYPT_CSFLE=true|false
```

Adicionar em:
- `src/common/environment/types.ts` — type definition
- `.env.example` do communication-service
- Infra/deployment config (ECS task definition ou Kubernetes secrets)

---

## 12. Checklist de Implementação

### Fase 1 — communication-service (blocker das outras fases)

- [ ] Adicionar `conditionOutreachDrafts` ao `AVAILABLE_CONNECTION`
- [ ] Criar `condition-outreach-draft.schema.ts` (Zod)
- [ ] Criar `i-condition-outreach-draft.ts` (interface)
- [ ] Criar `i-condition-outreach-draft-repository.ts` (interface repo)
- [ ] Criar `model.ts` (Mongoose)
- [ ] Criar `condition-outreach-draft-repository.ts` (com Zod validation)
- [ ] Criar REST endpoints (router + use-cases)
- [ ] Adicionar env vars
- [ ] Escrever testes (schema + repository + endpoints)
- [ ] Configurar rota no api-gateway

### Fase 2 — mcp-gateway-service

- [ ] Adicionar `getRaw()` no `ApiGatewayClient` (para binário)
- [ ] Criar `get-file-content.ts` tool
- [ ] Criar `suggest-condition-destinations.ts` tool (com LLM)
- [ ] Criar `draft-condition-outreach.ts` tool
- [ ] Registrar as 3 novas tools nos respectivos `index.ts`
- [ ] Escrever testes das 3 novas tools

### Fase 3 — agent-service

- [ ] Criar `r2.tier1.ts`
- [ ] Criar `r2.ts` handler
- [ ] Criar `r2.md` soul prompt
- [ ] Criar `r7.tier1.ts`
- [ ] Criar `r7.ts` handler
- [ ] Criar `r7.md` soul prompt
- [ ] Atualizar `definitions.ts` (habilitar R2 e R7)
- [ ] Verificar/atualizar `confidence.ts`
- [ ] Escrever testes (R2 handler + Tier 1, R7 handler + Tier 1)

### Fase 4 — Validação

- [ ] Teste manual via Trigger API (R2 com documento real)
- [ ] Verificar tasks criadas com `conditionDetails`
- [ ] Verificar R7 dispara via `TASK_CREATED`
- [ ] Verificar doc matching e linking
- [ ] Verificar criação de drafts em `condition_outreach_drafts`
- [ ] Verificar items no Agent Inbox
- [ ] Teste E2E com PDF de condval real (anonimizado)
- [ ] Ajuste de prompts se necessário

---

*Este PRD é a fonte de verdade para a implementação do R2. Qualquer decisão arquitetural não coberta aqui deve ser discutida e documentada antes de implementar.*
