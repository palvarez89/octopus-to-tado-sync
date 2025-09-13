
## 🔄 Historical Sync (Optional)

By default, the workflow syncs the **last 7 days** of usage to Tado (scheduled weekly).  
You can also run a **manual historical sync** to backfill older Octopus data.

### Run a Historical Sync via GitHub Actions

1. Go to the **Actions** tab in your GitHub repo.  
2. Select the **Sync Octopus to Tado** workflow.  
3. Click **Run workflow**.  
4. Fill in the inputs:
   - **historical** → `true`  
   - **start_date** → your desired start date (`YYYY-MM-DD`)  
   - **end_date** → your desired end date (`YYYY-MM-DD`)  

Example:

```
historical: true
start_date: 2024-01-01
end_date: 2024-03-31
```

⚠️ If you set `historical = true` but leave dates blank, the workflow will fail with a clear error message.

### Run Locally (Optional)

If you prefer to run the script yourself:

```bash
python sync_octopus_tado.py --historical --start-date 2024-01-01 --end-date 2024-03-31
```

- With no arguments → runs the default **last 7 days** sync.  
- With `--historical` + dates → runs a full backfill.
