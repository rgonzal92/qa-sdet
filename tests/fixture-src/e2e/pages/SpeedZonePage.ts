import { Page } from '@playwright/test';

export class SpeedZonePage {
  constructor(private page: Page) {}
  zoneName = () => this.page.getByLabel('Zone name');
  speedLimit = () => this.page.getByLabel('Speed limit (km/h)');
  save = () => this.page.getByRole('button', { name: 'Save' });
  status = () => this.page.locator('#msg');
  async open() { await this.page.goto('/form.html'); }
}
