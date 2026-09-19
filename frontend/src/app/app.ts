import { Component } from '@angular/core';
import { MatBadgeModule } from '@angular/material/badge';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatToolbarModule } from '@angular/material/toolbar';
import { Sidebar } from './components/sidebar/sidebar';
import { ChatInterface } from './components/chat-interface/chat-interface';

@Component({
  selector: 'app-root',
  imports: [Sidebar, ChatInterface, MatBadgeModule, MatButtonModule, MatCardModule, MatChipsModule, MatIconModule, MatProgressSpinnerModule, MatTooltipModule, MatToolbarModule],
  templateUrl: './app.html',
  styleUrls: ['./app.css', './pnc-brand.css'],
})
export class App {
  insightsOpen = false;

  toggleInsights(): void {
    this.insightsOpen = !this.insightsOpen;
  }

  readonly alerts = [
    { title: 'Policy refresh required', detail: 'Information Security Policy expires in 12 days.', tone: 'warning' },
    { title: 'New source indexed', detail: 'Q3 Treasury Operating Plan is ready to ask.', tone: 'success' },
  ];
  readonly searches = ['Treasury management', 'Client onboarding', 'Risk appetite', 'Cybersecurity controls'];
  readonly experts = [
    { initials: 'JS', name: 'John Smith', specialty: 'Payments Platform' },
    { initials: 'SJ', name: 'Sarah Johnson', specialty: 'Fraud & Risk' },
    { initials: 'MP', name: 'Mike Patel', specialty: 'Core Systems' },
  ];
}
