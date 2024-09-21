# Octopus Energy & Tado Energy IQ Sync Tool

This repository contains a script to automatically sync your Octopus Energy
smart meter readings with Tado's Energy IQ feature. The workflow provided allows
you to set this up to run on a weekly basis using GitHub Actions, so your Tado
Energy IQ remains up-to-date without any manual effort.

**Note**: This tool is specifically oriented toward users with smart gas meters
from Octopus Energy.

## Features

- Automatically pulls your gas usage data from Octopus Energy.
- Syncs the data with Tado Energy IQ for better home energy management insights.
- Set up once, and it runs weekly via GitHub Actions.

## Setup Instructions

Follow these steps to configure the sync for your own Octopus Energy and Tado
accounts:

### 1. Fork This Repository

First, fork this repository to your own GitHub account. This will allow you to
customize the secrets specific to your accounts and run the workflow
independently.

### 2. Configure GitHub Secrets

In order to use the script, you'll need to provide credentials for both your
Tado account and Octopus Energy API. This is done through GitHub secrets.

1. Go to the **Settings** tab of your forked repository.
2. In the left-hand menu, select **Secrets and variables** > **Actions**.
3. Click **New repository secret** and add the following secrets:

| Secret Name              | Description                                                   |
|--------------------------|---------------------------------------------------------------|
| `TADO_EMAIL`             | The email address associated with your Tado account.          |
| `TADO_PASSWORD`          | The password for your Tado account.                           |
| `OCTOPUS_MPRN`           | Your gas MPRN (Meter Point Reference Number).                 |
| `OCTOPUS_GAS_SERIAL`     | The serial number of your gas meter.                          |
| `OCTOPUS_API_KEY`        | Your Octopus Energy API key. You can obtain this from the Octopus Energy developer portal (details below). |

### 3. Obtain Your Octopus Energy Details

To find your **API Key**, **Gas MPRN**, and **Gas Serial Number**, follow these
steps:

1. Log into your [Octopus Energy
account](https://octopus.energy/dashboard/new/accounts/personal-details/api-access).
2. Navigate to the "API Access" section of your account. Here, you'll find your
**API Key**.
3. Your **Gas MPRN** and **Gas Serial Number** can also be found in this
section.

These details are necessary to allow the script to pull your gas usage data from
Octopus Energy.

### 4. Enable the Workflow

The repository is already set up with a GitHub Actions workflow that runs the
sync script once a week. The workflow is located at
`.github/workflows/schedule_sync.yml`. After you’ve added your secrets, the
workflow will automatically begin running on schedule.

You can manually trigger the workflow by navigating to the **Actions** tab in
your repository and selecting the sync workflow.

### 5. (Optional) Customize the Schedule

By default, the workflow runs weekly. If you want to change the schedule:

1. Open the `.github/workflows/schedule_sync.yml` file in your repository.
2. Modify the schedule trigger under `on: schedule:` following [the cron
syntax](https://crontab.guru/) for the desired frequency.

For example, to run daily at midnight:

```yaml
on: schedule:
    - cron: '0 0 * * *' ```
```

### 6. Monitor the Workflow

You can check the status of the sync runs in the **Actions** tab of your GitHub
repository. Here, you can see past runs, their logs, and any errors that might
have occurred.

### Usage

The GitHub Actions workflow automatically runs the following script:

```bash python sync_octopus_tado.py \ --tado-email "${{ secrets.TADO_EMAIL }}" \
--tado-password "${{ secrets.TADO_PASSWORD }}" \ --mprn "${{
secrets.OCTOPUS_MPRN }}" \ --gas-serial-number "${{ secrets.OCTOPUS_GAS_SERIAL
}}" \ --octopus-api-key "${{ secrets.OCTOPUS_API_KEY }}"

```

The script will:

1. Fetch the most recent gas usage readings from your Octopus Energy account
using their API.
2. Sync these readings with Tado's Energy IQ to keep your gas consumption
insights up-to-date.

### Troubleshooting

- **Incorrect credentials**: If the script fails due to incorrect credentials,
  ensure that the email, password, MPRN, and serial number are accurate. Verify
that your Octopus API key is valid.
- **Workflow failures**: Detailed logs for each sync run can be found in the
  **Actions** tab of your repository. Use these logs to identify and
troubleshoot any issues.

### Contributions

Feel free to contribute to this project by opening issues or submitting pull
requests. Any improvements, bug fixes, or new feature suggestions are welcome!

### License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file
for details.
